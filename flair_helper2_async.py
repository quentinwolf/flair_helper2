import aiohttp
import asyncio
import asyncpraw
import asyncprawcore
import sqlite3
import yaml
import re
from datetime import datetime, timedelta
import time
import zlib
import base64
import json
import logging
import os
import concurrent.futures
from logging.handlers import TimedRotatingFileHandler
from asyncprawcore import ResponseException
from asyncprawcore import NotFound
from discord_webhook import DiscordWebhook, DiscordEmbed

debugmode = False
verbosemode = False

logs_dir = "logs/"
if not os.path.exists(logs_dir):
    os.makedirs(logs_dir)

errors_filename = f'{logs_dir}errors.log'
logging.basicConfig(filename=errors_filename, level=logging.DEBUG, format='%(asctime)s %(levelname)s: %(message)s', filemode='a')
errors_logger = logging.getLogger('errors')

logging.getLogger('aiohttp').setLevel(logging.CRITICAL)

usernotes_lock = asyncio.Lock()
database_lock = asyncio.Lock()


# Create local sqlite db to cache/store Wiki Configs for all subs ones bot moderates
def create_database():
    conn = sqlite3.connect('flair_helper_configs.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS configs
                 (subreddit TEXT PRIMARY KEY, config TEXT)''')
    conn.commit()
    conn.close()

async def cache_config(subreddit_name, config):
    async with database_lock:
        conn = sqlite3.connect('flair_helper_configs.db')
        c = conn.cursor()
        try:
            c.execute("INSERT OR REPLACE INTO configs VALUES (?, ?)", (subreddit_name, yaml.dump(config)))
            conn.commit()
        finally:
            conn.close()

def get_cached_config(subreddit_name):
    conn = sqlite3.connect('flair_helper_configs.db')
    c = conn.cursor()
    c.execute("SELECT config FROM configs WHERE subreddit = ?", (subreddit_name,))
    result = c.fetchone()
    conn.close()
    if result:
        return yaml.safe_load(result[0])  # Use yaml.safe_load instead of yaml.load
    return None


async def get_latest_wiki_revision(subreddit):
    try:
        # Use get_page to fetch the wiki page
        wiki_page = await subreddit.wiki.get_page("flair_helper")
        # Now you can iterate over the revisions of the page
        async for revision in wiki_page.revisions(limit=1):
            return revision  # Return the latest revision
    except Exception as e:
        # Handle exceptions appropriately
        print(f"Error fetching latest wiki revision: {e}")
    return None


async def check_mod_permissions(subreddit, mod_name):
    moderators = await subreddit.moderator()
    for moderator in moderators:
        if moderator.name == mod_name:
            mod_permissions = set(moderator.mod_permissions)
            #print(f"Debugging: Mod {mod_name} has the following permissions in /r/{subreddit.display_name}: {mod_permissions}") if debugmode else None
            return mod_permissions
    #print(f"Debugging: Mod {mod_name} is not a moderator of /r/{subreddit.display_name}") if debugmode else None
    return None



async def fetch_and_cache_configs(reddit, max_retries=2, retry_delay=5, single_sub=None):
    create_database()
    moderated_subreddits = []
    if single_sub:
        moderated_subreddits.append(await reddit.subreddit(single_sub))
    else:
        async for subreddit in reddit.user.moderator_subreddits():
            moderated_subreddits.append(subreddit)

    me = await reddit.user.me()  # Correctly await the user object
    bot_username = me.name  # Now you can safely access the name attribute

    for subreddit in moderated_subreddits:
        subreddit_name = subreddit.display_name
        if f"u_{bot_username}" in subreddit_name:
            print(f"Skipping bot's own user page: /r/{subreddit_name}") if debugmode else None
            continue  # Skip the bot's own user page

        retries = 0
        while retries < max_retries:
            try:
                try:
                    # Access wiki page using asyncpraw
                    wiki_page = await subreddit.wiki.get_page('flair_helper')
                    wiki_content = wiki_page.content_md.strip()
                    # The rest of your code to handle the wiki content goes here
                except Exception as e:
                    # Handle exceptions appropriately
                    print(f"Error accessing wiki page: {e}")

                if not wiki_content:
                    print(f"Flair Helper configuration for /r/{subreddit_name} is blank. Skipping...") if debugmode else None
                    break  # Skip processing if the wiki page is blank

                try:
                    updated_config = yaml.load(wiki_content, Loader=yaml.FullLoader)
                    cached_config = get_cached_config(subreddit_name)

                    if cached_config != updated_config:
                        # Check if the mod who edited the wiki page has the "config" permission
                        if updated_config.get('require_config_to_edit', False):
                            wiki_revision = await get_latest_wiki_revision(subreddit)
                            mod_name = wiki_revision['author']

                            mod_permissions = await check_mod_permissions(subreddit, mod_name)
                            if mod_permissions is not None and ('all' in mod_permissions or 'config' in mod_permissions):
                                # The moderator has the 'config' permission or 'all' permissions
                                pass
                            else:
                                # The moderator does not have the 'config' permission or is not a moderator
                                error_output = f"Mod {mod_name} does not have permission to edit config in /r/{subreddit_name}"
                                print(error_output) if debugmode else None
                                errors_logger.error(error_output)
                                break  # Skip reloading the configuration and continue with the next subreddit

                        try:
                            yaml.load(wiki_content, Loader=yaml.FullLoader)
                            await cache_config(subreddit_name, updated_config)
                            print(f"The Flair Helper wiki page configuration for /r/{subreddit_name} has been successfully reloaded.") if debugmode else None
                            try:
                                subreddit_instance = await reddit.subreddit(subreddit_name)
                                await subreddit_instance.modmail.create(
                                    subject="Flair Helper Configuration Reloaded",
                                    body=f"The Flair Helper configuration for /r/{subreddit_name} has been successfully reloaded.",
                                    recipient=subreddit_name  # Ensure recipient is correctly specified, might need adjustment
                                )
                            except asyncpraw.exceptions.RedditAPIException as e:
                                error_output = f"Error sending modmail to /r/{subreddit_name}: {e}"
                                print(error_output) if debugmode else None
                                errors_logger.error(error_output)
                        except yaml.YAMLError as e:
                            error_output = f"Error parsing YAML configuration for /r/{subreddit_name}: {e}"
                            print(error_output) if debugmode else None
                            errors_logger.error(error_output)
                            try:
                                subreddit_instance = await reddit.subreddit(subreddit_name)
                                await subreddit_instance.modmail.create(
                                    subject="Flair Helper Configuration Error",
                                    body=f"The Flair Helper configuration for /r/{subreddit_name} could not be reloaded due to YAML parsing errors:\n\n{e}",
                                    recipient=subreddit_name
                                )
                            except asyncpraw.exceptions.RedditAPIException as e:
                                error_output = f"Error sending modmail to /r/{subreddit_name}: {e}"
                                print(error_output) if debugmode else None
                                errors_logger.error(error_output)
                    else:
                        print(f"The Flair Helper wiki page configuration for /r/{subreddit_name} has not changed.") if debugmode else None
                    break  # Configuration loaded successfully, exit the retry loop
                except (asyncprawcore.exceptions.ResponseException, asyncprawcore.exceptions.RequestException) as e:
                    error_output = f"Error loading configuration for /r/{subreddit_name}: {e}"
                    print(error_output) if debugmode else None
                    errors_logger.error(error_output)
                    retries += 1
                    if retries < max_retries:
                        print(f"Retrying in {retry_delay} seconds...") if debugmode else None
                        time.sleep(retry_delay)
                    else:
                        print(f"Max retries exceeded for /r/{subreddit_name}. Skipping...") if debugmode else None
            except asyncprawcore.exceptions.Forbidden:
                error_output = f"Error: Bot does not have permission to access the wiki page in /r/{subreddit_name}"
                print(error_output) if debugmode else None
                errors_logger.error(error_output)
                break  # Skip retrying if the bot doesn't have permission
            except asyncprawcore.exceptions.NotFound:
                error_output = f"Flair Helper wiki page doesn't exist for /r/{subreddit_name}"
                print(error_output) if debugmode else None
                errors_logger.error(error_output)
                try:
                    subreddit_instance = await reddit.subreddit(subreddit_name)
                    await subreddit_instance.modmail.create(
                        subject="Flair Helper Wiki Page Not Found",
                        body=f"The Flair Helper wiki page doesn't exist for /r/{subreddit_name}. Please go to https://www.reddit.com/r/{subreddit_name}/wiki/flair_helper and create the page to add this subreddit.",
                        recipient=subreddit_name
                    )
                except asyncpraw.exceptions.RedditAPIException as e:
                    error_output = f"Error sending modmail to /r/{subreddit_name}: {e}"
                    print(error_output) if debugmode else None
                    errors_logger.error(error_output)
                break  # Skip retrying if the wiki page doesn't exist


# Toolbox Note Handlers
def decompress_notes(compressed):
    try:
        decompressed = zlib.decompress(base64.b64decode(compressed))
        return json.loads(decompressed.decode('utf-8'))
    except (zlib.error, base64.binascii.Error, json.JSONDecodeError) as e:
        error_output = f"Error decompressing usernotes: {e}"
        print(error_output) if debugmode else None
        errors_logger.error(error_output)
        return {}

def compress_notes(notes):
    compressed = base64.b64encode(zlib.compress(json.dumps(notes).encode('utf-8'))).decode('utf-8')
    return compressed

async def update_usernotes(subreddit, author, note_text, link, mod_name):
    async with usernotes_lock:
        try:
            usernotes_wiki = await subreddit.wiki.get_page("usernotes")
            usernotes_content = usernotes_wiki.content_md  # Use the 'content_md' attribute without 'await'
            usernotes_data = json.loads(usernotes_content)  # Parse the JSON content

            if 'blob' not in usernotes_data:
                usernotes_data['blob'] = ''

            decompressed_notes = decompress_notes(usernotes_data['blob'])

            timestamp = int(time.time())  # Get the current timestamp

            if 'constants' not in usernotes_data:
                usernotes_data['constants'] = {'users': []}

            if mod_name not in usernotes_data['constants']['users']:
                usernotes_data['constants']['users'].append(mod_name)

            mod_index = usernotes_data['constants']['users'].index(mod_name)

            add_usernote(decompressed_notes, author, note_text, link, mod_index)

            usernotes_data['blob'] = compress_notes(decompressed_notes)

            compressed_notes = json.dumps(usernotes_data)
            edit_reason = f"note {timestamp} added on user {author} via flair_helper2"
            await usernotes_wiki.edit(content=compressed_notes, reason=edit_reason)

        except Exception as e:
            # Handle exceptions as needed
            print(f"Error updating usernotes: {e}")


def add_usernote(notes, author, note_text, link, mod_index):
    if author not in notes:
        notes[author] = {"ns": []}

    timestamp = int(time.time())
    submission_id = link.split('/')[-3]
    new_note = {
        "n": f"[FH] {note_text}",
        "t": timestamp,
        "m": mod_index,
        "l": f"l,{submission_id}",
        "w": 0
    }
    notes[author]["ns"].append(new_note)


async def send_modmail(subreddit, subject, message):
    await subreddit.message(subject, message)


def send_webhook_notification(config, post, flair_text, mod_name, flair_guid):
    print(f"Sending webhook notification for flair GUID: {flair_guid}") if debugmode else None
    if 'webhook' in config and flair_guid in config['send_to_webhook']:
        print(f"Webhook notification triggered for flair GUID: {flair_guid}") if debugmode else None

        webhook_url = config['webhook']
        webhook = DiscordWebhook(url=webhook_url)

        # Create the embed
        embed = DiscordEmbed(title=f"{post.title}", url="https://www.reddit.com"+post.permalink, description="Post Flaired: "+post.link_flair_text, color=242424)
        embed.add_embed_field(name="Author", value=post.author.name)
        embed.add_embed_field(name="Score", value=post.score)
        embed.add_embed_field(name="Created", value=datetime.utcfromtimestamp(post.created_utc).strftime('%b %u %Y %H:%M:%S UTC'))
        embed.add_embed_field(name="User Flair", value=flair_text)
        embed.add_embed_field(name="Subreddit", value="/r/"+post.subreddit.display_name)

        if not config.get('wh_exclude_mod', False):
            embed.add_embed_field(name="Actioned By", value=mod_name, inline=False)

        if not config.get('wh_exclude_reports', False):
            reports = ", ".join(post.mod_reports)
            embed.add_embed_field(name="Reports", value=reports)

        if post.over_18 and not config.get('wh_include_nsfw_images', False):
            pass  # Exclude NSFW images unless explicitly included
        elif not config.get('wh_exclude_image', False):
            embed.set_image(url=post.url)

        # Add the embed to the webhook
        webhook.add_embed(embed)

        # Set the content if provided
        if 'wh_content' in config:
            webhook.set_content(config['wh_content'])

        # Send a ping if the score exceeds the specified threshold
        if 'wh_ping_over_score' in config and 'wh_ping_over_ping' in config:
            if post.score >= config['wh_ping_over_score']:
                if config['wh_ping_over_ping'] == 'everyone':
                    webhook.set_content("@everyone")
                elif config['wh_ping_over_ping'] == 'here':
                    webhook.set_content("@here")
                else:
                    webhook.set_content(f"<@&{config['wh_ping_over_ping']}>")

        # Send the webhook
        response = webhook.execute()

# Async function to fetch a user's current flair in a subreddit
async def fetch_user_flair(subreddit, username):
    async for flair in subreddit.flair(redditor=username):
        #print(f"Flair: {flair}") if debugmode else None
        return flair  # Return the first (and presumably only) flair setting
    #print(f"flair: None") if debugmode else None
    return None  # If no flair is set

# Primary process to handle any flair changes that appear in the logs
async def process_flair_assignment(reddit, log_entry, config, subreddit):
    target_fullname = log_entry.target_fullname
    if target_fullname.startswith('t3_'):  # Check if it's a submission
        submission_id = target_fullname[3:]  # Remove the 't3_' prefix
        post = await reddit.submission(submission_id)
        flair_guid = getattr(post, 'link_flair_template_id', None)  # Use getattr to safely retrieve the attribute
        print(f"Flair GUID detected: {flair_guid}")
        # Reload the configuration from the database
        config = get_cached_config(subreddit.display_name)
        if flair_guid and flair_guid in config['flairs']:

            await post.load()
            # Now that post data is loaded, ensure that author data is loaded
            if post.author:
                await post.author.load()
                author_id = post.author.id
            else:
                # Handle the case where the post may not have an author (e.g., deleted account)
                author_id = None

            if post.subreddit:
                await post.subreddit.load()
                subreddit_id = post.subreddit.id
            else:
                # Handle the case where the post may not have an author (e.g., deleted account)
                subreddit_id = None

            current_flair = await fetch_user_flair(subreddit, post.author.name)  # Fetch the current flair asynchronously

            # Initialize defaults if the user has no current flair
            flair_text = ''
            flair_css_class = ''

            if current_flair:
                flair_text = current_flair.get('flair_text', '')
                flair_css_class = current_flair.get('flair_css_class', '')

            # Retrieve the flair details from the configuration
            flair_details = config['flairs'][flair_guid]

            utc_offset = config.get('utc_offset', 0)
            custom_time_format = config.get('custom_time_format', '')

            now = datetime.utcnow() + timedelta(hours=utc_offset)
            created_time = datetime.utcfromtimestamp(post.created_utc) + timedelta(hours=utc_offset)
            actioned_time = datetime.utcfromtimestamp(log_entry.created_utc) + timedelta(hours=utc_offset)

            placeholders = {
                'time_unix': int(now.timestamp()),
                'time_iso': now.isoformat(),
                'time_custom': now.strftime(custom_time_format) if custom_time_format else '',
                'created_unix': int(created_time.timestamp()),
                'created_iso': created_time.isoformat(),
                'created_custom': created_time.strftime(custom_time_format) if custom_time_format else '',
                'actioned_unix': int(actioned_time.timestamp()),
                'actioned_iso': actioned_time.isoformat(),
                'actioned_custom': actioned_time.strftime(custom_time_format) if custom_time_format else ''
            }

            # Create a dictionary to store the placeholder values
            placeholders.update({
                'author': post.author.name,
                'subreddit': post.subreddit.display_name,
                'body': post.selftext,
                'title': post.title,
                'id': post.id,
                'permalink': post.permalink,
                'url': post.permalink,
                'domain': post.domain,
                'link': post.url,
                'kind': 'submission',
                'mod': log_entry.mod.name,
                'author_flair_text': post.author_flair_text if post.author_flair_text else '',
                'author_flair_css_class': post.author_flair_css_class if post.author_flair_css_class else '',
                'author_flair_template_id': post.author_flair_template_id if post.author_flair_template_id else '',
                'link_flair_text': post.link_flair_text if post.link_flair_text else '',
                'link_flair_css_class': post.link_flair_css_class if post.link_flair_css_class else '',
                'link_flair_template_id': post.link_flair_template_id if post.link_flair_template_id else '',
                'author_id': post.author.id,
                'subreddit_id': post.subreddit.id
            })

            # Format the header, flair details, and footer with the placeholders
            formatted_header = config['header']
            formatted_flair_details = flair_details
            formatted_footer = config['footer']

            skip_add_newlines = config.get('skip_add_newlines', False)
            require_config_to_edit = config.get('require_config_to_edit', False)
            ignore_same_flair_seconds = config.get('ignore_same_flair_seconds', 60)

            if not skip_add_newlines:
                formatted_header += "\n\n"
                formatted_footer = "\n\n" + formatted_footer

            if require_config_to_edit and not log_entry.mod.has_permission('config'):
                error_output = f"Mod {log_entry.mod.name} does not have permission to edit config"
                print(error_output) if debugmode else None
                errors_logger.error(error_output)
                return

            last_flair_time = getattr(post, '_last_flair_time', 0)
            if time.time() - last_flair_time < ignore_same_flair_seconds:
                print(f"Ignoring same flair action within {ignore_same_flair_seconds} seconds") if debugmode else None
                return
            post._last_flair_time = time.time()

            for placeholder, value in placeholders.items():
                formatted_header = formatted_header.replace(f"{{{{{placeholder}}}}}", str(value))
                formatted_flair_details = formatted_flair_details.replace(f"{{{{{placeholder}}}}}", str(value))
                formatted_footer = formatted_footer.replace(f"{{{{{placeholder}}}}}", str(value))

            removal_reason = f"{formatted_header}\n\n{formatted_flair_details}\n\n{formatted_footer}"

            # Execute the configured actions
            if 'approve' in config and config['approve'].get(flair_guid, False):
                print(f"Approve triggered in /r/{subreddit.display_name}") if debugmode else None
                print(f"Submission approved in /r/{subreddit.display_name}") if debugmode else None
                await post.mod.approve()
                print(f"Submission unlocked in /r/{subreddit.display_name}") if debugmode else None
                await post.mod.unlock()
                print(f"Spoiler removed in /r/{subreddit.display_name}") if debugmode else None
                await post.mod.unspoiler()

            if 'remove' in config and config['remove'].get(flair_guid, False):
                print(f"remove triggered in /r/{subreddit.display_name}") if debugmode else None
                mod_note = config['usernote'].get(flair_guid, '')
                await post.mod.remove(spam=False, mod_note=mod_note)

            if 'comment' in config and config['comment'].get(flair_guid, False):
                post_age_days = (datetime.utcnow() - datetime.utcfromtimestamp(post.created_utc)).days
                max_age = config.get('max_age_for_comment', 175) if 'max_age_for_comment' in config else 175
                if isinstance(max_age, dict):
                    max_age = max_age.get(flair_guid, 175)
                if post_age_days <= max_age:
                    print(f"comment triggered in /r/{subreddit.display_name}") if debugmode else None

                    if 'remove' in config and config['remove'].get(flair_guid, False):
                        # If both 'remove' and 'comment' are configured for the flair GUID
                        removal_type = config.get('removal_comment_type', 'public_as_subreddit') if 'removal_comment_type' in config else 'public_as_subreddit'

                        print(f"Debugging: post_id={post.id}, removal_reason={removal_reason}, removal_type={removal_type}") if debugmode else None

                        try:
                            await post.mod.send_removal_message(message=removal_reason, type=removal_type)
                        except asyncpraw.exceptions.RedditAPIException as e:
                            print(f"Error sending removal message: {e}") if debugmode else None
                    else:
                        # If only 'comment' is configured for the flair GUID
                        try:
                            comment = await post.reply(removal_reason)
                            if 'comment_stickied' in config and config['comment_stickied'].get(flair_guid, True):
                                await comment.mod.distinguish(sticky=True)
                            if 'comment_locked' in config and config['comment_locked'].get(flair_guid, True):
                                await comment.mod.lock()
                        except asyncpraw.exceptions.RedditAPIException as e:
                            print(f"Error replying with comment: {e}") if debugmode else None

            # Check if banning is configured for the flair GUID
            if 'bans' in config and flair_guid in config['bans']:
                ban_duration = config['bans'][flair_guid]
                ban_message = config['ban_message'].get(flair_guid) if 'ban_message' in config else None
                ban_note = config['ban_note'].get(flair_guid) if 'ban_note' in config else None

                print(f"Debugging: ban_duration={ban_duration}, ban_message={ban_message}, ban_note={ban_note}") if debugmode else None

                if ban_message:
                    for placeholder, value in placeholders.items():
                        ban_message = ban_message.replace(f"{{{{{placeholder}}}}}", str(value))

                if ban_duration is True:
                    print(f"permanent ban triggered in /r/{subreddit.display_name}") if debugmode else None
                    await subreddit.banned.add(post.author, ban_message=ban_message, ban_reason=ban_note)
                elif isinstance(ban_duration, int) and ban_duration > 0:
                    print(f"temporary ban triggered for {ban_duration} days in /r/{subreddit.display_name}") if debugmode else None
                    try:
                        await subreddit.banned.add(post.author, ban_message=ban_message, ban_reason=ban_note, duration=ban_duration)
                    except Exception as e:
                        print(f"Error banning user: {e}") if debugmode else None
                else:
                    print(f"banning not triggered for flair GUID: {flair_guid} in /r/{subreddit.display_name}") if debugmode else None

            if 'unbans' in config and flair_guid in config['unbans']:
                print(f"unban triggered in /r/{subreddit.display_name}") if debugmode else None
                try:
                    await subreddit.banned.remove(post.author)
                except Exception as e:
                    print(f"Error unbanning user: {e}") if debugmode else None

            if 'lock_post' in config and config['lock_post'].get(flair_guid, False):
                print(f"lock triggered in /r/{subreddit.display_name}") if debugmode else None
                await post.mod.lock()

            if 'spoiler_post' in config and config['spoiler_post'].get(flair_guid, False):
                print(f"spoiler triggered in /r/{subreddit.display_name}") if debugmode else None
                await post.mod.spoiler()

            if 'set_author_flair_text' in config and config['set_author_flair_text'].get(flair_guid) or 'set_author_flair_css_class' in config and config['set_author_flair_css_class'].get(flair_guid):
                print(f"set_author_flair triggered in /r/{subreddit.display_name}") if debugmode else None

                print(f"Current flair: text='{flair_text}', css_class='{flair_css_class}'") if debugmode else None
                # Update the flair text based on the configuration
                if 'set_author_flair_text' in config and config['set_author_flair_text'].get(flair_guid):
                    new_flair_text = config['set_author_flair_text'][flair_guid]
                    flair_text = new_flair_text.replace('{{author_flair_text}}', flair_text)
                    print(f"Updating flair text to: '{flair_text}'") if debugmode else None

                # Update the flair CSS class based on the configuration
                if 'set_author_flair_css_class' in config and config['set_author_flair_css_class'].get(flair_guid):
                    new_flair_css_class = config['set_author_flair_css_class'][flair_guid]
                    flair_css_class = new_flair_css_class.replace('{{author_flair_css_class}}', flair_css_class)
                    print(f"Updating flair CSS class to: '{flair_css_class}'") if debugmode else None

                # Set the updated flair for the user
                try:
                    await subreddit.flair.set(post.author.name, text=flair_text, css_class=flair_css_class)
                    print(f"Flair updated for user {post.author.name}: text='{flair_text}', css_class='{flair_css_class}'") if debugmode else None
                except Exception as e:
                    print(f"Error updating flair for user {post.author.name}: {e}") if debugmode else None

            if 'usernote' in config and config['usernote'].get(flair_guid):
                print(f"usernote triggered in /r/{subreddit.display_name}") if debugmode else None
                author = post.author.name
                note_text = config['usernote'][flair_guid]
                link = post.permalink
                mod_name = log_entry.mod.name

                await update_usernotes(subreddit, author, note_text, link, mod_name)

            if 'remove_link_flair' in config and 'remove_link_flair' in config and flair_guid in config['remove_link_flair']:
                print(f"remove_link_flair triggered in /r/{subreddit.display_name}") if debugmode else None
                await post.mod.flair(text='', css_class='')

            if 'add_contributor' in config and flair_guid in config['add_contributor']:
                print(f"add_contributor triggered in /r/{subreddit.display_name}") if debugmode else None
                try:
                    await subreddit.contributor.add(post.author)
                except asyncpraw.exceptions.RedditAPIException as e:
                    print(f"Error adding contributor: {e}") if debugmode else None

            if 'remove_contributor' in config and flair_guid in config['remove_contributor']:
                print(f"remove_contributor triggered in /r/{subreddit.display_name}") if debugmode else None
                try:
                    await subreddit.contributor.remove(post.author)
                except asyncpraw.exceptions.RedditAPIException as e:
                    print(f"Error removing contributor: {e}") if debugmode else None

            if  'send_to_webhook' in config and 'send_to_webhook' in config and flair_guid in config['send_to_webhook']:
                print(f"send_to_webhook triggered in /r/{subreddit.display_name}") if debugmode else None
                # Send webhook notification
                send_webhook_notification(config, post, flair_text, log_entry.mod.name, flair_guid)


# Handle Private Messages to allow the bot to reply back with a list of flairs for convenience
async def handle_private_messages(reddit):
    async for message in reddit.inbox.unread(limit=None):
        if isinstance(message, asyncpraw.models.Message):
            subject = message.subject.lower()
            body = message.body.strip()
            subreddit_name = body.split()[0]

            print(f"PM Received for {subreddit_name}") if debugmode else None

            if not re.match(r'^[a-zA-Z0-9_]{3,21}$', subreddit_name):
                response = "Invalid subreddit name. The subreddit name must be between 3 and 21 characters long and can only contain letters, numbers, and underscores."
            else:
                try:
                    subreddit = await reddit.subreddit(subreddit_name)
                    await subreddit.load()  # Load the subreddit data

                    if subject == 'list':
                        print(f"'list' PM Received for {subreddit_name}") if debugmode else None
                        if subreddit.user_is_moderator:  # Use the property directly
                            mod_flair_templates = [
                                f"{template['text']}: {template['id']}"
                                async for template in subreddit.flair.link_templates
                                if template['mod_only']
                            ]
                            if mod_flair_templates:
                                response = f"Mod-only flair templates for /r/{subreddit_name}:\n\n" + "\n\n".join(mod_flair_templates)
                            else:
                                response = f"No mod-only flair templates found for /r/{subreddit_name}."
                        else:
                            response = f"You are not a moderator of /r/{subreddit_name}."

                    elif subject == 'auto':
                        try:
                            print(f"'auto' PM Received for {subreddit_name}") if debugmode else None
                            if subreddit.user_is_moderator:  # Use the property directly

                                # Filter for mod-only flair templates
                                flair_templates = [
                                    template async for template in subreddit.flair.link_templates
                                    if template['mod_only']
                                ]

                                config = {
                                    'header': "Hi /u/{{author}}, thanks for contributing to /r/{{subreddit}}. Unfortunately, your post was removed as it violates our rules:",
                                    'footer': "Please read the sidebar and the rules of our subreddit [here](https://www.reddit.com/r/{{subreddit}}/about/rules) before posting again. If you have any questions or concerns please [message the moderators through modmail](https://www.reddit.com/message/compose?to=/r/{{subreddit}}&subject=About my removed {{kind}}&message=I'm writing to you about the following {{kind}}: {{url}}. %0D%0DMy issue is...).",
                                    'flairs': {},
                                    'remove': {},
                                    'lock_post': {},
                                    'spoiler': {},
                                    'comment': {},
                                    'removal_comment_type': 'public_as_subreddit',
                                    'usernote': {},
                                    'usernote_type_name': 'flair_helper_note'
                                }

                                for template in flair_templates:
                                    flair_id = template['id']
                                    flair_text = template['text']
                                    config['flairs'][flair_id] = f"Removal violation: {flair_text}"
                                    config['remove'][flair_id] = True
                                    config['lock_post'][flair_id] = True
                                    config['spoiler'][flair_id] = True
                                    config['comment'][flair_id] = True
                                    config['usernote'][flair_id] = f"Post violated rule: {flair_text}"

                                yaml_output = yaml.dump(config, sort_keys=False)
                                formatted_yaml_output = "    " + yaml_output.replace("\n", "\n    ")

                                response = f"Here's a sample Flair Helper 2 configuration for /r/{subreddit_name} which you can place in [https://www.reddit.com/r/{subreddit_name}/wiki/flair_helper](https://www.reddit.com/r/{subreddit_name}/wiki/flair_helper)\n\n"
                                response += formatted_yaml_output
                                response += "\n\nPlease be sure to review all the detected flairs and remove any that may not be applicable (such as Mod Announcements, Notices, News, etc.)"

                                # Check if the response exceeds the 10k character limit
                                while len(response) > 10000:
                                    print(f"Response length > 10000 and is currently {len(response)}, removing extra entries") if debugmode else None

                                    # Get the list of flair IDs
                                    flair_ids = list(config['flairs'].keys())

                                    # Check if the response exceeds the 10k character limit
                                    while len(response) > 10000:
                                        print(f"Response length > 10000 and is currently {len(response)}, removing extra entries") if debugmode else None

                                        # Get the list of flair IDs from the action sections
                                        action_flair_ids = list(config['remove'].keys())

                                        # Check if there are any flair IDs left to remove from the action sections
                                        if action_flair_ids:
                                            # Remove the last flair ID from the action sections
                                            last_flair_id = action_flair_ids.pop()

                                            if last_flair_id in config['remove']:
                                                del config['remove'][last_flair_id]
                                            if last_flair_id in config['lock_post']:
                                                del config['lock_post'][last_flair_id]
                                            if last_flair_id in config['spoiler']:
                                                del config['spoiler'][last_flair_id]
                                            if last_flair_id in config['comment']:
                                                del config['comment'][last_flair_id]
                                            if last_flair_id in config['usernote']:
                                                del config['usernote'][last_flair_id]
                                        else:
                                            # If there are no more flair IDs to remove from the action sections, break the loop
                                            break

                                        # Regenerate the YAML output and response
                                        yaml_output = yaml.dump(config, sort_keys=False)
                                        formatted_yaml_output = "    " + yaml_output.replace("\n", "\n    ")

                                        response = f"Here's a sample Flair Helper 2 configuration for /r/{subreddit_name} which you can place in [https://www.reddit.com/r/{subreddit_name}/wiki/flair_helper](https://www.reddit.com/r/{subreddit_name}/wiki/flair_helper)\n\n"
                                        response += formatted_yaml_output
                                        response += "\n\nPlease be sure to review all the detected flairs and remove any that may not be applicable (such as Mod Announcements, Notices, News, etc.)"

                                print(f"\n\nFormatted Yaml Output Message:\n\n{response}") if debugmode else None
                            else:
                                response = f"You are not a moderator of /r/{subreddit_name}."
                        except asyncprawcore.exceptions.NotFound:
                            response = f"Subreddit /r/{subreddit_name} not found."

                    else:
                        response = "Unknown command. Available commands: 'list', 'auto'."

                except asyncprawcore.exceptions.NotFound:
                    response = f"Subreddit /r/{subreddit_name} not found."

            await message.mark_read()
            try:
                await message.reply(response)
            except Exception as e:
                error_output = f"Error replying to message: {e}"
                print(error_output) if debugmode else None
                errors_logger.error(error_output)



# Primary Mod Log Monitor
async def monitor_mod_log(reddit, subreddit, config):
    try:
        async for log_entry in subreddit.mod.stream.log(skip_existing=True):
            print(f"New log entry: {log_entry.action}") if verbosemode else None
            if log_entry.action == 'editflair':
                print(f"Flair action detected in /r/{subreddit.display_name}") if debugmode else None
                if log_entry.target_fullname:
                    await process_flair_assignment(reddit, log_entry, config, subreddit)  # Ensure process_flair_assignment is also async
                else:
                    print(f"No target found") if debugmode else None
            elif log_entry.action == 'wikirevise':
                if 'flair_helper' in log_entry.details:
                    print(f"Flair Helper wiki page revised in /r/{subreddit.display_name}") if debugmode else None
                    try:
                        await fetch_and_cache_configs(reddit, max_retries=2, retry_delay=5, single_sub=subreddit.display_name)  # Make sure fetch_and_cache_configs is async
                    except asyncprawcore.exceptions.NotFound:
                        error_output = f"Flair Helper wiki page not found in /r/{subreddit.display_name}"
                        print(error_output) if debugmode else None
                        errors_logger.error(error_output)
            else:
                print(f"Ignoring action: {log_entry.action} in /r/{subreddit.display_name}") if verbosemode else None
    except asyncprawcore.exceptions.ResponseException as e:
        error_output = f"Error: {e}"
        print(error_output) if debugmode else None
        errors_logger.error(error_output)




# Create Multithreaded Instance to monitor all subs that have a valid Flair_Helper configuration
async def run_bot_async(reddit):
    # Correctly await the asynchronous function call
    await fetch_and_cache_configs(reddit)  # This is adapted to be async

    moderated_subreddits = []
    async for sub in reddit.user.moderator_subreddits():
        moderated_subreddits.append(sub)

    me = await reddit.user.me()  # Correctly await the user object
    bot_username = me.name  # Now you can safely access the name attribute

    tasks = []
    for subreddit in moderated_subreddits:
        subreddit_name = subreddit.display_name
        if f"u_{bot_username}" in subreddit_name:
            print(f"Skipping bot's own user page: /r/{subreddit_name}") if debugmode else None
            continue  # Skip the bot's own user page

        config = get_cached_config(subreddit_name)  # This seems like a synchronous operation

        if config:
            print(f"Monitoring mod log for /r/{subreddit_name}")
            task = asyncio.create_task(monitor_mod_log(reddit, subreddit, config))
            tasks.append(task)
        else:
            print(f"No Flair Helper configuration found for /r/{subreddit_name}")

    if tasks:
        await asyncio.gather(*tasks)


# Check for PM's every 60 seconds
async def monitor_private_messages(reddit):
    while True:
        await handle_private_messages(reddit)
        await asyncio.sleep(60)  # Sleep for 60 seconds before the next iteration


async def main():
    async with aiohttp.ClientSession() as session:
        reddit = asyncpraw.Reddit("fh2_login", requestor_kwargs={"session": session})

        # Create separate tasks for run_bot_async and monitor_private_messages
        bot_task = asyncio.create_task(run_bot_async(reddit))
        pm_task = asyncio.create_task(monitor_private_messages(reddit))

        # Run both tasks concurrently using asyncio.gather
        await asyncio.gather(bot_task, pm_task)

if __name__ == "__main__":
    asyncio.run(main())
