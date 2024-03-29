import praw
import prawcore
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
from prawcore.exceptions import ResponseException
from prawcore.exceptions import NotFound
from discord_webhook import DiscordWebhook, DiscordEmbed

debugmode = False
verbosemode = False

auto_accept_mod_invites = False

discord_bot_notifications = False
discord_webhook_url = "DISCORD_WEBHOOK_URL"

logs_dir = "logs/"
if not os.path.exists(logs_dir):
    os.makedirs(logs_dir)

errors_filename = f'{logs_dir}errors.log'
logging.basicConfig(filename=errors_filename, level=logging.DEBUG, format='%(asctime)s %(levelname)s: %(message)s')
errors_logger = logging.getLogger('errors')

# Create a Reddit instance using PRAW
reddit = praw.Reddit("fh2_login")

def error_handler(error_message, notify_discord=False):
    print(error_message) if debugmode else None
    errors_logger.error(error_message)
    if notify_discord:
        discord_status_notification(error_message)

# Create local sqlite db to cache/store Wiki Configs for all subs ones bot moderates
def create_database():
    conn = sqlite3.connect('flair_helper_configs.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS configs
                 (subreddit TEXT PRIMARY KEY, config TEXT)''')
    conn.commit()
    conn.close()

def cache_config(subreddit_name, config):
    conn = sqlite3.connect('flair_helper_configs.db')
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO configs VALUES (?, ?)", (subreddit_name, yaml.dump(config)))
    conn.commit()
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


def discord_status_notification(message):
    current_utc_timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    if discord_bot_notifications:
        try:
            webhook = DiscordWebhook(url=discord_webhook_url)
            embed = DiscordEmbed(title="Flair Helper 2 Status Notification", description=message, color=242424)
            webhook.add_embed(embed)
            response = webhook.execute()
            print(f"{current_utc_timestamp}: Discord status notification sent: {message}") if debugmode else None
        except Exception as e:
            print(f"{current_utc_timestamp}: Error sending Discord status notification: {str(e)}") if debugmode else None


def fetch_and_cache_configs(bot_username, max_retries=2, retry_delay=5, single_sub=None):
    current_utc_timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    create_database()
    moderated_subreddits = [reddit.subreddit(single_sub)] if single_sub else list(reddit.user.moderator_subreddits())

    for subreddit in moderated_subreddits:
        if f"u_{bot_username}" in subreddit.display_name:
            print(f"{current_utc_timestamp}: Skipping bot's own user page: /r/{subreddit.display_name}") if debugmode else None
            continue  # Skip the bot's own user page

        retries = 0
        while retries < max_retries:
            try:
                wiki_page = subreddit.wiki['flair_helper']
                wiki_content = wiki_page.content_md.strip()

                if not wiki_content:
                    print(f"{current_utc_timestamp}: Flair Helper configuration for /r/{subreddit.display_name} is blank. Skipping...") if debugmode else None
                    break  # Skip processing if the wiki page is blank

                try:
                    updated_config = yaml.load(wiki_content, Loader=yaml.FullLoader)
                    cached_config = get_cached_config(subreddit.display_name)

                    if cached_config != updated_config:
                        # Check if the mod who edited the wiki page has the "config" permission
                        if updated_config.get('require_config_to_edit', False):
                            wiki_revision = list(subreddit.wiki['flair_helper'].revisions(limit=1))[0]
                            mod_name = wiki_revision['author']
                            mod = reddit.redditor(mod_name)
                            if not mod.has_permission('config', subreddit=subreddit):
                                error_handler(f"Mod {mod_name} does not have permission to edit config in /r/{subreddit.display_name}\n\nMod {mod_name} has the following permissions in /r/{subreddit.display_name}: {mod_permissions}", notify_discord=True)

                                continue  # Skip reloading the configuration

                        try:
                            yaml.load(wiki_content, Loader=yaml.FullLoader)
                            cache_config(subreddit.display_name, updated_config)
                            error_handler(f"The Flair Helper wiki page configuration for /r/{subreddit.display_name} has been successfully cached and reloaded.", notify_discord=True)

                            sleep(2)  # Adjust the delay as needed

                            try:
                                reddit.subreddit(subreddit.display_name).message(
                                    subject="Flair Helper Configuration Reloaded",
                                    message=f"The Flair Helper configuration for /r/{subreddit.display_name} has been successfully reloaded."
                                )
                            except praw.exceptions.RedditAPIException as e:
                                error_handler(f"Error sending modmail to /r/{subreddit.display_name}: {e}", notify_discord=True)
                        except yaml.YAMLError as e:
                            error_handler(f"Error parsing YAML configuration for /r/{subreddit.display_name}: {e}", notify_discord=True)
                            try:
                                reddit.subreddit(subreddit.display_name).message(
                                    subject="Flair Helper Configuration Error",
                                    message=f"The Flair Helper configuration for /r/{subreddit.display_name} could not be reloaded due to YAML parsing errors:\n\n{e}"
                                )
                            except praw.exceptions.RedditAPIException as e:
                                error_handler(f"Error sending modmail to /r/{subreddit.display_name}: {e}", notify_discord=True)
                    else:
                        print(f"{current_utc_timestamp}: The Flair Helper wiki page configuration for /r/{subreddit.display_name} has not changed.") if debugmode else None
                    break  # Configuration loaded successfully, exit the retry loop
                except (prawcore.exceptions.ResponseException, prawcore.exceptions.RequestException) as e:
                    error_handler(f"Error loading configuration for /r/{subreddit.display_name}: {e}", notify_discord=True)
                    retries += 1
                    if retries < max_retries:
                        print(f"{current_utc_timestamp}: Retrying in {retry_delay} seconds...") if debugmode else None
                        time.sleep(retry_delay)
                    else:
                        print(f"{current_utc_timestamp}: Max retries exceeded for /r/{subreddit.display_name}. Skipping...") if debugmode else None
            except prawcore.exceptions.Forbidden:
                error_handler(f"Error: Bot does not have permission to access the wiki page in /r/{subreddit.display_name}", notify_discord=True)
                break  # Skip retrying if the bot doesn't have permission
            except prawcore.exceptions.NotFound:
                error_handler(f"Flair Helper wiki page doesn't exist for /r/{subreddit.display_name}", notify_discord=True)
                try:
                    reddit.subreddit(subreddit.display_name).message(
                        subject="Flair Helper Wiki Page Not Found",
                        message=f"The Flair Helper wiki page doesn't exist for /r/{subreddit.display_name}. Please go to https://www.reddit.com/r/{subreddit.display_name}/wiki/flair_helper and create the page to add this subreddit.  You can send me a PM with 'list' or 'auto' to generate a sample configuration.\n\n[Generate a List of Flairs](https://www.reddit.com/message/compose?to=/u/{bot_username}&subject=list&message={subreddit.display_name})\n\n[Auto-Generate a sample Flair Helper Config](https://www.reddit.com/message/compose?to=/u/{bot_username}&subject=auto&message={subreddit.display_name})\n\nYou can find more information in the Flair Helper documentation on /r/Flair_Helper2/wiki/tutorial/ \n\nHappy Flairing!"
                    )
                except praw.exceptions.RedditAPIException as e:
                    error_handler(f"Error sending modmail to /r/{subreddit.display_name}: {e}", notify_discord=True)
                break  # Skip retrying if the wiki page doesn't exist

# Toolbox Note Handlers
def decompress_notes(compressed):
    try:
        decompressed = zlib.decompress(base64.b64decode(compressed))
        return json.loads(decompressed.decode('utf-8'))
    except (zlib.error, base64.binascii.Error, json.JSONDecodeError) as e:
        error_handler(f"Error decompressing usernotes: {e}", notify_discord=True)
        return {}

def compress_notes(notes):
    compressed = base64.b64encode(zlib.compress(json.dumps(notes).encode('utf-8'))).decode('utf-8')
    return compressed

def update_usernotes(subreddit, author, note_text, link, mod_name):
        try:
            usernotes_wiki = subreddit.wiki.get_page("usernotes")
            usernotes_content = usernotes_wiki.content_md
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
            usernotes_wiki.edit(content=compressed_notes, reason=edit_reason)

        except Exception as e:
            error_handler(f"update_usernotes: Error updating usernotes: {e}", notify_discord=True)

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

def send_webhook_notification(config, post, flair_text, mod_name, flair_guid):
    current_utc_timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    print(f"{current_utc_timestamp}: Sending webhook notification for flair GUID: {flair_guid}") if debugmode else None
    if 'webhook' in config and flair_guid in config['send_to_webhook']:
        print(f"{current_utc_timestamp}: Webhook notification triggered for flair GUID: {flair_guid}") if debugmode else None

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

# Primary process to handle any flair changes that appear in the logs
def process_flair_assignment(log_entry, config, subreddit):
    current_utc_timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    target_fullname = log_entry.target_fullname
    if target_fullname.startswith('t3_'):  # Check if it's a submission
        submission_id = target_fullname[3:]  # Remove the 't3_' prefix
        post = reddit.submission(submission_id)
        flair_guid = post.link_flair_template_id
        # Get the post title and author for debugging
        post_author_name = post.author.name if post.author else "[deleted]"
        print(f"{current_utc_timestamp}: Flair GUID {flair_guid} detected on ID: {submission_id} on post '{post.title}' by {post_author_name} in /r/{subreddit.display_name}") if debugmode else None
        # boolean variable to track whether the author is deleted or suspended:
        is_author_deleted_or_suspended = post_author_name == "[deleted]"

        # Reload the configuration from the database
        config = get_cached_config(subreddit.display_name)
        if flair_guid in config['flairs']:

            # Retrieve the flair details from the configuration
            flair_details = config['flairs'][flair_guid]

            if post.author:
                if hasattr(post.author, 'is_suspended') and post.author.is_suspended:
                    author_id = None
                    print(f"{current_utc_timestamp}: Skipping author ID on ID: {submission_id} for suspended user: {post.author.name}") if debugmode else None
                else:
                    author_id = post.author.id
            else:
                # Handle the case where the post may not have an author (e.g., deleted account)
                author_id = None

            if post.subreddit:
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

            # Format the header, flair details, and footer with the placeholders
            formatted_header = config['header']
            formatted_footer = config['footer']

            skip_add_newlines = config.get('skip_add_newlines', False)
            require_config_to_edit = config.get('require_config_to_edit', False)
            ignore_same_flair_seconds = config.get('ignore_same_flair_seconds', 60)

            if not skip_add_newlines:
                formatted_header += "\n\n"
                formatted_footer = "\n\n" + formatted_footer

            last_flair_time = getattr(post, '_last_flair_time', 0)
            if time.time() - last_flair_time < ignore_same_flair_seconds:
                print(f"{current_utc_timestamp}: Ignoring same flair action within {ignore_same_flair_seconds} seconds") if debugmode else None
                return
            post._last_flair_time = time.time()

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
                'author': post_author_name,
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
                'author_id': author_id,
                'subreddit_id': post.subreddit.id
            })

            for placeholder, value in placeholders.items():
                formatted_header = formatted_header.replace(f"{{{{{placeholder}}}}}", str(value))
                formatted_flair_details = flair_details.replace(f"{{{{{placeholder}}}}}", str(value))
                formatted_footer = formatted_footer.replace(f"{{{{{placeholder}}}}}", str(value))

            removal_reason = f"{formatted_header}\n\n{formatted_flair_details}\n\n{formatted_footer}"


            # Execute the configured actions
            if 'approve' in config and config['approve'].get(flair_guid, False):
                print(f"{current_utc_timestamp}: Approve triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                print(f"{current_utc_timestamp}: Submission approved on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                post.mod.approve()
                print(f"{current_utc_timestamp}: Submission unlocked on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                post.mod.unlock()
                print(f"{current_utc_timestamp}: Spoiler removed on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                post.mod.unspoiler()

            if 'remove' in config and config['remove'].get(flair_guid, False):
                print(f"{current_utc_timestamp}: remove triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                mod_note = config['usernote'].get(flair_guid, '')
                post.mod.remove(spam=False, mod_note=mod_note)

            if 'comment' in config and config['comment'].get(flair_guid, False):
                post_age_days = (datetime.utcnow() - datetime.utcfromtimestamp(post.created_utc)).days
                max_age = config.get('max_age_for_comment', 175) if 'max_age_for_comment' in config else 175
                if isinstance(max_age, dict):
                    max_age = max_age.get(flair_guid, 175)
                if post_age_days <= max_age:
                    print(f"{current_utc_timestamp}: comment triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None

                    if 'remove' in config and config['remove'].get(flair_guid, False):
                        # If both 'remove' and 'comment' are configured for the flair GUID
                        removal_type = config.get('removal_comment_type', 'public_as_subreddit') if 'removal_comment_type' in config else 'public_as_subreddit'
                        #print(f"Debugging: post_id={post.id}, removal_reason={removal_reason}, removal_type={removal_type}") if debugmode else None
                        try:
                            post.mod.send_removal_message(message=removal_reason, type=removal_type)
                        except praw.exceptions.RedditAPIException as e:
                            error_handler(f"process_flair_assignment: Error sending removal message in /r/{subreddit.display_name}: {e}", notify_discord=True)
                    else:
                        # If only 'comment' is configured for the flair GUID
                        try:
                            comment = post.reply(removal_reason)
                            if 'comment_stickied' in config and config['comment_stickied'].get(flair_guid, True):
                                comment.mod.distinguish(sticky=True)
                            if 'comment_locked' in config and config['comment_locked'].get(flair_guid, True):
                                comment.mod.lock()
                        except praw.exceptions.RedditAPIException as e:
                            error_handler(f"process_flair_assignment: Error replying with comment in /r/{subreddit.display_name}: {e}", notify_discord=True)

            if 'lock_post' in config and config['lock_post'].get(flair_guid, False):
                print(f"{current_utc_timestamp}: lock triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                post.mod.lock()

            if 'spoiler_post' in config and config['spoiler_post'].get(flair_guid, False):
                print(f"{current_utc_timestamp}: spoiler triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                post.mod.spoiler()

            if 'remove_link_flair' in config and 'remove_link_flair' in config and flair_guid in config['remove_link_flair']:
                print(f"{current_utc_timestamp}: remove_link_flair triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                post.mod.flair(text='', css_class='')

            if  'send_to_webhook' in config and 'send_to_webhook' in config and flair_guid in config['send_to_webhook']:
                print(f"{current_utc_timestamp}: send_to_webhook triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                # Send webhook notification
                send_webhook_notification(config, post, flair_text, log_entry.mod.name, flair_guid)


            # Only process the below if not suspended or deleted
            if not is_author_deleted_or_suspended:

                # Check if banning is configured for the flair GUID
                if 'bans' in config and flair_guid in config['bans']:
                    ban_duration = config['bans'][flair_guid]
                    ban_message = config['ban_message'].get(flair_guid) if 'ban_message' in config else None
                    ban_note = config['ban_note'].get(flair_guid) if 'ban_note' in config else None

                    print(f"{current_utc_timestamp}: Debugging: ban_duration={ban_duration}, ban_message={ban_message}, ban_note={ban_note}") if debugmode else None

                    if ban_message:
                        for placeholder, value in placeholders.items():
                            ban_message = ban_message.replace(f"{{{{{placeholder}}}}}", str(value))

                    if ban_duration is True:
                        print(f"{current_utc_timestamp}: permanent ban triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                        subreddit.banned.add(post.author, ban_message=ban_message, ban_reason=ban_note)
                    elif isinstance(ban_duration, int) and ban_duration > 0:
                        print(f"{current_utc_timestamp}: temporary ban triggered on ID: {submission_id} for {ban_duration} days in /r/{subreddit.display_name}") if debugmode else None
                        try:
                            subreddit.banned.add(post.author, ban_message=ban_message, ban_reason=ban_note, duration=ban_duration)
                        except Exception as e:
                            error_handler(f"process_flair_assignment: Error banning user {post.author.name} in /r/{subreddit.display_name}: {e}", notify_discord=True)
                    else:
                        print(f"{current_utc_timestamp}: banning not triggered on ID: {submission_id} for flair GUID: {flair_guid} in /r/{subreddit.display_name}") if debugmode else None

                if 'unbans' in config and flair_guid in config['unbans']:
                    print(f"{current_utc_timestamp}: unban triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                    try:
                        subreddit.banned.remove(post.author)
                    except Exception as e:
                        error_handler(f"process_flair_assignment: Error unbanning user {post.author.name} in /r/{subreddit.display_name}: {e}", notify_discord=True)

                if 'set_author_flair_text' in config and config['set_author_flair_text'].get(flair_guid) or 'set_author_flair_css_class' in config and config['set_author_flair_css_class'].get(flair_guid):
                    print(f"{current_utc_timestamp}: set_author_flair triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                    current_flair = next(subreddit.flair(post.author.name))
                    flair_text = current_flair['flair_text'] if current_flair else ''
                    flair_css_class = current_flair['flair_css_class'] if current_flair else ''

                    if 'set_author_flair_text' in config and config['set_author_flair_text'].get(flair_guid):
                        new_flair_text = config['set_author_flair_text'][flair_guid]
                        flair_text = new_flair_text.replace('{{author_flair_text}}', str(flair_text))
                        print(f"{current_utc_timestamp}: Updating flair text to: '{flair_text}'") if debugmode else None

                    if 'set_author_flair_css_class' in config and config['set_author_flair_css_class'].get(flair_guid):
                        new_flair_css_class = config['set_author_flair_css_class'][flair_guid]
                        flair_css_class = new_flair_css_class.replace('{{author_flair_css_class}}', str(flair_css_class))
                        print(f"{current_utc_timestamp}: Updating flair CSS class to: '{flair_css_class}'") if debugmode else None

                    try:
                        subreddit.flair.set(post.author.name, text=flair_text, css_class=flair_css_class)
                        print(f"{current_utc_timestamp}: Flair updated for user {post.author.name}: text='{flair_text}', css_class='{flair_css_class}'") if debugmode else None
                    except Exception as e:
                        error_handler(f"process_flair_assignment: Error updating flair for {post.author.name} in /r/{subreddit.display_name}: {e}", notify_discord=True)

                if  'usernote' in config and config['usernote'].get(flair_guid):
                    print(f"{current_utc_timestamp}: usernote triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                    author = post.author.name
                    note_text = config['usernote'][flair_guid]
                    link = post.permalink
                    mod_name = log_entry.mod.name

                    update_usernotes(subreddit, author, note_text, link, mod_name)

                if 'add_contributor' in config and flair_guid in config['add_contributor']:
                    print(f"{current_utc_timestamp}: add_contributor triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                    try:
                        subreddit.contributor.add(post.author)
                    except praw.exceptions.RedditAPIException as e:
                        error_handler(f"process_flair_assignment: Error adding contributor in /r/{subreddit.display_name}: {e}", notify_discord=True)

                if 'remove_contributor' in config and flair_guid in config['remove_contributor']:
                    print(f"{current_utc_timestamp}: remove_contributor triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                    try:
                        subreddit.contributor.remove(post.author)
                    except praw.exceptions.RedditAPIException as e:
                        error_handler(f"process_flair_assignment: Error removing contributor in /r/{subreddit.display_name}: {e}", notify_discord=True)



# Handle Private Messages to allow the bot to reply back with a list of flairs for convenience
def handle_private_messages():
    current_utc_timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    for message in reddit.inbox.unread(limit=None):
        if isinstance(message, praw.models.Message):

            if message.subject.lower() == 'invitation to moderate':
                if auto_accept_mod_invites:
                    subreddit = reddit.subreddit(message.subreddit.display_name)
                    subreddit.mod.accept_invite()
                    print(f"{current_utc_timestamp}: Accepted mod invitation for r/{subreddit.display_name}") if debugmode else None
                    discord_status_notification(f"Accepted mod invitation for r/{subreddit.display_name}")
                else:
                    print(f"{current_utc_timestamp}: Received mod invitation for r/{message.subreddit.display_name} but auto-accept is disabled") if debugmode else None
                    discord_status_notification(f"Received mod invitation for r/{message.subreddit.display_name} but auto-accept is disabled")

            else:
                body = message.body.strip()
                subreddit_name = body.split()[0]

                print(f"{current_utc_timestamp}: PM Received for {subreddit_name}") if debugmode else None

                if not re.match(r'^[a-zA-Z0-9_]{3,21}$', subreddit_name):
                    response = "Invalid subreddit name. The subreddit name must be between 3 and 21 characters long and can only contain letters, numbers, and underscores."
                else:
                    try:
                        subreddit = reddit.subreddit(subreddit_name)
                        if subject == 'list':
                            print(f"{current_utc_timestamp}: 'list' PM Received for {subreddit_name}") if debugmode else None
                            if subreddit.user_is_moderator:  # Use the property directly
                                mod_flair_templates = [
                                    f"{template['text']}: {template['id']}"
                                    for template in subreddit.flair.link_templates
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
                                print(f"{current_utc_timestamp}: 'auto' PM Received for {subreddit_name}") if debugmode else None
                                if subreddit.user_is_moderator:  # Use the property directly

                                    response = create_auto_flairhelper_wiki(reddit, subreddit, mode="pm")

                                else:
                                    response = f"You are not a moderator of /r/{subreddit_name}."
                            except prawcore.exceptions.NotFound:
                                response = f"Subreddit /r/{subreddit_name} not found."

                        else:
                            response = "Unknown command. Available commands: 'list', 'auto'."

                    except prawcore.exceptions.NotFound:
                        response = f"Subreddit /r/{subreddit_name} not found."

            message.mark_read()
            try:
                message.reply(response)
            except Exception as e:
                error_handler(f"handle_private_messages: Error replying to message: {e}", notify_discord=True)


def create_auto_flairhelper_wiki(reddit, subreddit, mode):
    # Filter for mod-only flair templates
    flair_templates = [
        template for template in subreddit.flair.link_templates
        if template['mod_only']
    ]

    comment = """### This is an Auto-Generated Configuration. Please review it carefully, all options are 'False' by default to prevent an automatic configuration from causing troubles.\n### Please add additional settings as required, and enable what you wish.\n### You may also remove excess lines that you do not need, everything does not explicitly need to be defined as 'False'\n### If something isn't set in this config, it won't be processed by default.\n"""

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
        config['remove'][flair_id] = False
        config['lock_post'][flair_id] = False
        config['spoiler'][flair_id] = False
        config['comment'][flair_id] = False
        config['usernote'][flair_id] = f"Removed: {flair_text}"

    yaml_output = yaml.dump(config, sort_keys=False, allow_unicode=True, width=float("inf"))

    if mode == "pm":
        formatted_yaml_output = "    " + yaml_output.replace("\n", "\n    ")

        final_output = f"Here's a sample Flair Helper 2 configuration for /r/{subreddit.display_name} which you can place in [https://www.reddit.com/r/{subreddit.display_name}/wiki/flair_helper](https://www.reddit.com/r/{subreddit.display_name}/wiki/flair_helper)\n\nBy default all options are set to 'false' if you wish to enable that specific action for a particular flair, change it to 'true'"
        final_output += comment + formatted_yaml_output
        final_output += "\n\nPlease be sure to review all the detected flairs and remove any that may not be applicable (such as Mod Announcements, Notices, News, etc.)"

        # Implement the 10,000 character limit on the complete response for private messages
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
                yaml_output = yaml.dump(config, sort_keys=False, allow_unicode=True, width=float("inf"))
                formatted_yaml_output = "    " + yaml_output.replace("\n", "\n    ")

                final_output = f"Here's a sample Flair Helper 2 configuration for /r/{subreddit.display_name} which you can place in [https://www.reddit.com/r/{subreddit.display_name}/wiki/flair_helper](https://www.reddit.com/r/{subreddit.display_name}/wiki/flair_helper)\n\nBy default all options are set to 'false' if you wish to enable that specific action for a particular flair, change it to 'true'"
                final_output += comment + formatted_yaml_output
                final_output += "\n\nPlease be sure to review all the detected flairs and remove any that may not be applicable (such as Mod Announcements, Notices, News, etc.)"


    elif mode == "wiki":
        final_output = comment + yaml_output

    print(f"\n\nFormatted Yaml Output Message:\n\n{yaml_output}\n\n") if debugmode else None

    return final_output


def check_new_mod_invitations(reddit):
    current_utc_timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    me = reddit.user.me()
    bot_username = me.name

    while True:
        current_subreddits = [sub for sub in reddit.user.moderator_subreddits()]
        stored_subreddits = get_stored_subreddits()

        new_subreddits = [sub for sub in current_subreddits if sub.display_name not in stored_subreddits]

        for subreddit in new_subreddits:
            if f"u_{bot_username}" in subreddit.display_name:
                print(f"{current_utc_timestamp}: Skipping bot's own user page: /r/{subreddit.display_name}") if debugmode else None
                continue  # Skip the bot's own user page

            subreddit_instance = reddit.subreddit(subreddit.display_name)

            max_retries = 3
            retry_delay = 5  # Delay in seconds between retries

            for attempt in range(max_retries):
                try:
                    wiki_page = subreddit.wiki.get_page('flair_helper')
                    wiki_content = wiki_page.content_md.strip()

                    if not wiki_content:
                        # Flair Helper wiki page exists but is blank
                        auto_gen_config = create_auto_flairhelper_wiki(reddit, subreddit, mode="wiki")
                        subreddit.wiki.create('flair_helper', auto_gen_config)
                        print(f"{current_utc_timestamp}: Created auto_gen_config for 'flair_helper' wiki page for /r/{subreddit.display_name}") if debugmode else None

                        subject = f"Flair Helper Configuration Needed for /r/{subreddit.display_name}"
                        message = f"Hi! I noticed that I was recently added as a moderator to /r/{subreddit.display_name}.\n\nThe Flair Helper wiki page here: /r/{subreddit.display_name}/wiki/flair_helper exists but was currently blank.  I've went ahead and generated a working config based upon your 'Mod Only' flairs you have configured.  Otherwise, you can send me a PM with 'list' or 'auto' to generate a sample configuration.\n\n[Generate a List of Flairs](https://www.reddit.com/message/compose?to=/u/{bot_username}&subject=list&message={subreddit.display_name})\n\n[Auto-Generate a sample Flair Helper Config](https://www.reddit.com/message/compose?to=/u/{bot_username}&subject=auto&message={subreddit.display_name})\n\nYou can find more information in the Flair Helper documentation on /r/Flair_Helper2/wiki/tutorial/ \n\nHappy Flairing!"
                        subreddit_instance.message(subject, message)
                        print(f"{current_utc_timestamp}: Sent PM to /r/{subreddit.display_name} moderators to create a Flair Helper configuration (wiki page exists but is blank)") if debugmode else None
                    else:
                        # Flair Helper wiki page exists and has content
                        fetch_and_cache_configs(reddit, max_retries=2, retry_delay=5, single_sub=subreddit.display_name)
                        print(f"{current_utc_timestamp}: Fetched and cached configuration for /r/{subreddit.display_name}") if debugmode else None
                    break

                except prawcore.exceptions.NotFound:
                    # Flair Helper wiki page doesn't exist
                    auto_gen_config = create_auto_flairhelper_wiki(reddit, subreddit, mode="wiki")
                    subreddit.wiki.create('flair_helper', auto_gen_config)
                    print(f"{current_utc_timestamp}: Created auto_gen_config for 'flair_helper' wiki page for /r/{subreddit.display_name}") if debugmode else None

                    subject = f"Flair Helper Configuration Needed for /r/{subreddit.display_name}"
                    message = f"Hi! I noticed that I was recently added as a moderator to /r/{subreddit.display_name}. To use my Flair Helper features, please setup your configuration on the newly created 'flair_helper' wiki page here: /r/{subreddit.display_name}/wiki/flair_helper \n\nI've went ahead and generated a working config based upon your 'Mod Only' flairs you have configured.  Otherwise, you can send me a PM with 'list' or 'auto' to generate a sample configuration.\n\n[Generate a List of Flairs](https://www.reddit.com/message/compose?to=/u/{bot_username}&subject=list&message={subreddit.display_name})\n\n[Auto-Generate a sample Flair Helper Config](https://www.reddit.com/message/compose?to=/u/{bot_username}&subject=auto&message={subreddit.display_name})\n\nYou can find more information in the Flair Helper documentation on /r/Flair_Helper2/wiki/tutorial/ \n\nHappy Flairing!"
                    subreddit_instance.message(subject, message)
                    print(f"{current_utc_timestamp}: Sent PM to /r/{subreddit.display_name} moderators to create a Flair Helper configuration (wiki page created)") if debugmode else None

                except praw.exceptions.RedditAPIException as e:
                    if e.error_type == "RATELIMIT":
                        wait_time_match = re.search(r"for (\d+) minute", e.message)
                        if wait_time_match:
                            wait_minutes = int(wait_time_match.group(1))
                            print(f"{current_utc_timestamp}: Rate limited. Waiting for {wait_minutes} minutes before retrying.") if debugmode else None
                            discord_status_notification(f"check_new_mod_invitations Rate Limited for /r/{subreddit.display_name}.  Waiting for {wait_minutes} minutes before retrying.")
                            sleep(wait_minutes * 60 + retry_delay)
                            # After waiting, you might need to retry the operation that triggered the rate limit
                        else:
                            print("Rate limited, but could not extract wait time.") if debugmode else None
                            discord_status_notification(f"check_new_mod_invitations Rate limited for /r/{subreddit.display_name}, but could not extract wait time.")
                            sleep(retry_delay)  # Wait for a default delay before retrying

                    else:
                        error_handler(f"check_new_mod_invitations: Reddit API Exception in /r/{subreddit.display_name}: {e}", notify_discord=True)
                        break

        sleep(3600)  # Check for new mod invitations every hour (adjust as needed)




# Primary Mod Log Monitor
def monitor_mod_log(subreddit, config, bot_username):
    current_utc_timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    accounts_to_ignore = ['AssistantBOT1', 'anyadditionalacctshere', 'thatmayinteractwithflair']
    try:
        for log_entry in subreddit.mod.stream.log(skip_existing=True):
            print(f"{current_utc_timestamp}: New log entry: {log_entry.action}") if verbosemode else None
            if log_entry.action == 'wikirevise':
                if 'flair_helper' in log_entry.details:
                    print(f"{current_utc_timestamp}: Flair Helper wiki page revised by {log_entry.mod} in /r/{log_entry.subreddit}") if debugmode else None
                    try:
                        fetch_and_cache_configs(bot_username, max_retries=2, retry_delay=5, single_sub=subreddit.display_name)
                    except prawcore.exceptions.NotFound:
                        error_output = f"monitor_mod_log: Flair Helper wiki page not found in /r/{subreddit.display_name}"
                        print(error_output) if debugmode else None
                        errors_logger.error(error_output)
            elif (log_entry.action == 'editflair'
              and log_entry.mod not in accounts_to_ignore
              and log_entry.target_fullname.startswith('t3_')):
                # This is a link (submission) flair edit
                if log_entry.target_fullname:
                    # Get the post object
                    print(f"{current_utc_timestamp}: Flair action detected by {log_entry.mod} in /r/{log_entry.subreddit}") if debugmode else None
                    process_flair_assignment(log_entry, config, subreddit)
                else:
                    print(f"{current_utc_timestamp}: No target found") if debugmode else None
            else:
                print(f"{current_utc_timestamp}: Ignoring action: {log_entry.action} in /r/{subreddit.display_name}") if verbosemode else None
    except praw.exceptions.RedditAPIException as e:
        if e.error_type == "RATELIMIT":
            wait_time_match = re.search(r"for (\d+) minute", e.message)
            if wait_time_match:
                wait_minutes = int(wait_time_match.group(1))
                print(f"Rate limited. Waiting for {wait_minutes} minutes before retrying.")
                sleep(wait_minutes * 60)
                # After waiting, you might need to retry the operation that triggered the rate limit
            else:
                print("Rate limited, but could not extract wait time.")
                sleep(60)  # Wait for a default duration before retrying
        else:
            error_handler(f"monitor_mod_log: Error in /r/{subreddit.display_name}: {e}", notify_discord=True)




# Create Multithreaded Instance to monitor all subs that have a valid Flair_Helper configuration
def run_bot():
    fetch_and_cache_configs()
    moderated_subreddits = list(reddit.user.moderator_subreddits())
    bot_username = reddit.user.me().name

    with concurrent.futures.ThreadPoolExecutor() as flair_executor:
        flair_futures = []
        for subreddit in moderated_subreddits:
            if f"u_{bot_username}" in subreddit.display_name:
                print(f"Skipping bot's own user page: /r/{subreddit.display_name}") if debugmode else None
                continue  # Skip the bot's own user page

            config = get_cached_config(subreddit.display_name)

            if config:
                print(f"Valid Config Exists for /r/{subreddit.display_name}.  Flair Helper 2 Active.")
                future = flair_executor.submit(monitor_mod_log, subreddit, config, bot_username)
                flair_futures.append(future)
            else:
                print(f"No Flair Helper configuration found for /r/{subreddit.display_name}")

        # Wait for all the flair-related futures to complete
        concurrent.futures.wait(flair_futures)

# Check for PM's every 60 seconds
def monitor_private_messages():
    while True:
        handle_private_messages()
        time.sleep(60)  # Sleep for 60 seconds before the next iteration


def main():
    discord_status_notification("Flair Helper 2 has started up successfully!")

    with concurrent.futures.ThreadPoolExecutor() as executor:
        executor.submit(run_bot)
        executor.submit(monitor_private_messages)
        executor.submit(check_new_mod_invitations)

if __name__ == "__main__":
    main()
