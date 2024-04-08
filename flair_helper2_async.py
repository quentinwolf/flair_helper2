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
import traceback
import concurrent.futures
from logging.handlers import TimedRotatingFileHandler
from asyncprawcore import ResponseException
from asyncprawcore import NotFound
from discord_webhook import DiscordWebhook, DiscordEmbed

debugmode = False
verbosemode = False

auto_accept_mod_invites = False
send_pm_on_wiki_config_update = False

discord_bot_notifications = False
discord_webhook_url = "YOUR_DISCORD_WEBHOOK_URL"

logs_dir = "logs/"
if not os.path.exists(logs_dir):
    os.makedirs(logs_dir)

errors_filename = f'{logs_dir}errors.log'
logging.basicConfig(filename=errors_filename, level=logging.WARNING, format='%(asctime)s %(levelname)s: %(message)s', filemode='a')
errors_logger = logging.getLogger('errors')

logging.getLogger('aiohttp').setLevel(logging.CRITICAL)

usernotes_lock = asyncio.Lock()
database_lock = asyncio.Lock()


async def error_handler(error_message, notify_discord=False):
    print(error_message) if debugmode else None
    errors_logger.error(error_message)
    if notify_discord:
        await discord_status_notification(error_message)


subreddit_cache = {}

def populate_subreddit_cache_from_db():
    subreddits = get_stored_subreddits()
    for subreddit_name in subreddits:
        subreddit_cache[subreddit_name] = subreddit_name

async def get_subreddit(reddit, subreddit_name):
    if subreddit_name in subreddit_cache:
        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: get_subreddit: subreddit_name in subreddit_cache: subreddit_name: {subreddit_name}") if debugmode else None
        return subreddit_cache[subreddit_name]
    else:
        subreddit = await reddit.subreddit(subreddit_name)
        subreddit_cache[subreddit_name] = subreddit
        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: get_subreddit: subreddit_name NOT in subreddit_cache: subreddit_name: {subreddit_name}, subreddit: {subreddit}") if debugmode else None
        return subreddit


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
            c.execute("INSERT OR REPLACE INTO configs VALUES (?, ?)", (subreddit_name, json.dumps(config, sort_keys=True)))
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
        try:
            return json.loads(result[0])  # Use json.loads instead of yaml.safe_load
        except json.JSONDecodeError:
            return None
    return None

def get_stored_subreddits():
    conn = sqlite3.connect('flair_helper_configs.db')
    c = conn.cursor()
    c.execute("SELECT subreddit FROM configs")
    stored_subreddits = [row[0] for row in c.fetchall()]
    conn.close()
    return stored_subreddits

async def get_latest_wiki_revision(subreddit):
    try:
        # Use get_page to fetch the wiki page
        wiki_page = await subreddit.wiki.get_page("flair_helper")
        # Now you can iterate over the revisions of the page
        async for revision in wiki_page.revisions(limit=1):
            return revision  # Return the latest revision
    except Exception as e:
        # Handle exceptions appropriately
        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Error fetching latest wiki revision: {e}")
    return None


def convert_yaml_to_json(yaml_config):
    # Create the GeneralConfiguration section
    general_config = {
        "GeneralConfiguration": {
            "notes": yaml_config.get('notes', ''),
            "header": yaml_config.get('header', ''),
            "footer": yaml_config.get('footer', ''),
            "usernote_type_name": yaml_config.get('usernote_type_name', ''),
            "removal_comment_type": yaml_config.get('removal_comment_type', ''),
            "skip_add_newlines": yaml_config.get('skip_add_newlines', False),
            "require_config_to_edit": yaml_config.get('require_config_to_edit', False),
            "ignore_same_flair_seconds": yaml_config.get('ignore_same_flair_seconds', 60),
            "webhook": yaml_config.get('webhook', ''),
            "wh_content": yaml_config.get('wh_content', ''),
            "wh_ping_over_score": yaml_config.get('wh_ping_over_score', None),
            "wh_ping_over_ping": yaml_config.get('wh_ping_over_ping', ''),
            "wh_exclude_mod": yaml_config.get('wh_exclude_mod', False),
            "wh_exclude_reports": yaml_config.get('wh_exclude_reports', False),
            "wh_exclude_image": yaml_config.get('wh_exclude_image', False),
            "wh_include_nsfw_images": yaml_config.get('wh_include_nsfw_images', False),
            "utc_offset": yaml_config.get('utc_offset', 0),
            "custom_time_format": yaml_config.get('custom_time_format', ''),
            "maxAgeForComment": yaml_config.get('max_age_for_comment', 175),
            "maxAgeForBan": yaml_config.get('max_age_for_ban', None)
        }
    }

    # Convert the YAML configuration to JSON format
    json_config = [general_config]
    for flair_id, flair_details in yaml_config.get('flairs', {}).items():
        # Remove special characters except dashes, underscores, periods, and commas
        cleaned_mod_note = re.sub(r'[^a-zA-Z0-9\s\-_.,]', '', yaml_config.get('ban_note', {}).get(flair_id, ''))

        # Truncate the mod_note to 250 characters
        cleaned_mod_note = cleaned_mod_note[:100]

        # Remove special characters except dashes, underscores, periods, and commas
        cleaned_modlog_reason = re.sub(r'[^a-zA-Z0-9\s\-_.,/\\]', '', yaml_config.get('ban', {}).get(flair_id, ''))
        cleaned_modlog_reason = cleaned_modlog_reason.replace('\n', ' ')
        cleaned_modlog_reason = cleaned_modlog_reason.replace('  ', ' ')
        cleaned_modlog_reason = cleaned_modlog_reason.replace('  ', ', ')
        cleaned_modlog_reason = cleaned_modlog_reason.strip()

        # Truncate the modlog_reason to 250 characters
        cleaned_modlog_reason = cleaned_modlog_reason[:250]

        flair_config = {
            "templateId": flair_id,
            "notes": flair_details,
            "approve": flair_id in yaml_config.get('approve', {}),
            "remove": flair_id in yaml_config.get('remove', {}),
            "lock": flair_id in yaml_config.get('lock_post', {}),
            "spoiler": flair_id in yaml_config.get('spoiler_post', {}),
            "clearPostFlair": flair_id in yaml_config.get('remove_link_flair', {}),
            "modlogReason": cleaned_modlog_reason,
            "comment": {
                "enabled": flair_id in yaml_config.get('comment', {}),
                "body": flair_details,
                "lockComment": yaml_config.get('comment_locked', {}).get(flair_id, False),
                "stickyComment": yaml_config.get('comment_stickied', {}).get(flair_id, False),
                "distinguish": True,
                "headerFooter": True
            },
            "nukeUserComments": flair_id in yaml_config.get('nukeUserComments', {}),
            "usernote": {
                "enabled": flair_id in yaml_config.get('usernote', {}),
                "note": yaml_config.get('usernote', {}).get(flair_id, '')
            },
            "contributor": {
                "enabled": flair_id in yaml_config.get('add_contributor', {}) or flair_id in yaml_config.get('remove_contributor', {}),
                "action": "add" if flair_id in yaml_config.get('add_contributor', {}) else "add"
            },
            "userFlair": {
                "enabled": flair_id in yaml_config.get('set_author_flair_text', {}) or flair_id in yaml_config.get('set_author_flair_css_class', {}),
                "text": yaml_config.get('set_author_flair_text', {}).get(flair_id, ''),
                "cssClass": yaml_config.get('set_author_flair_css_class', {}).get(flair_id, '')
            },
            "ban": {
                "enabled": flair_id in yaml_config.get('bans', {}),
                "duration": "" if yaml_config.get('bans', {}).get(flair_id, 0) is True else yaml_config.get('bans', {}).get(flair_id, 0),
                "message": yaml_config.get('ban_message', {}).get(flair_id, ''),
                "modNote": cleaned_mod_note
            },
            "unban": flair_id in yaml_config.get('unbans', {}),
            "sendToWebhook": flair_id in yaml_config.get('send_to_webhook', [])
        }

        json_config.append(flair_config)

    print(f"YAML to JSON Conversion complete.")

    return json_config



async def discord_status_notification(message):
    if discord_bot_notifications:
        try:
            webhook = DiscordWebhook(url=discord_webhook_url)
            embed = DiscordEmbed(title="Flair Helper 2 Status Notification", description=message, color=242424)
            webhook.add_embed(embed)
            response = webhook.execute()
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Discord status notification sent: {message}") if debugmode else None
        except Exception as e:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Error sending Discord status notification: {str(e)}") if debugmode else None


async def check_mod_permissions(subreddit, mod_name):
    moderators = await subreddit.moderator()
    for moderator in moderators:
        if moderator.name == mod_name:
            mod_permissions = set(moderator.mod_permissions)
            #print(f"Debugging: Mod {mod_name} has the following permissions in /r/{subreddit.display_name}: {mod_permissions}") if debugmode else None
            return mod_permissions
    #print(f"Debugging: Mod {mod_name} is not a moderator of /r/{subreddit.display_name}") if debugmode else None
    return None



async def fetch_and_cache_configs(reddit, bot_username, max_retries=3, retry_delay=1, max_retry_delay=60, single_sub=None):
    create_database()
    moderated_subreddits = []
    if single_sub:
        moderated_subreddits.append(await get_subreddit(reddit, single_sub))
    else:
        async for subreddit in reddit.user.moderator_subreddits():
            moderated_subreddits.append(subreddit)

    for subreddit in moderated_subreddits:
        if f"u_{bot_username}" in subreddit.display_name:
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
                    await error_handler(f"Error accessing the /r/{subreddit.display_name} flair_helper wiki page: {e}", notify_discord=True)

                if not wiki_content:
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Flair Helper configuration for /r/{subreddit.display_name} is blank. Skipping...") if debugmode else None
                    break  # Skip processing if the wiki page is blank

                try:
                    # Try parsing the content as JSON
                    updated_config = json.loads(wiki_content)
                except json.JSONDecodeError:
                    try:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Error parsing Flair Helper configuration as JSON for /r/{subreddit.display_name}.  Attempting YAML to JSON conversion...") if debugmode else None
                        # If JSON parsing fails, try parsing as YAML
                        updated_config = yaml.safe_load(wiki_content)
                        # Convert the YAML configuration to JSON format
                        updated_config = convert_yaml_to_json(updated_config)
                        # Save the JSON configuration back to the wiki page
                        wiki_page = await subreddit.wiki.get_page('flair_helper')
                        await wiki_page.edit(content=json.dumps(updated_config, indent=4))
                    except yaml.YAMLError:
                        # If both JSON and YAML parsing fail, send a notification to the subreddit
                        subject = f"Flair Helper Configuration Error in /r/{subreddit.display_name}"
                        message = (
                            f"The Flair Helper configuration for /r/{subreddit.display_name} is in an unsupported or invalid format.\n\n"
                            f"Please check the [flair_helper wiki page](https://www.reddit.com/r/{subreddit.display_name}/wiki/flair_helper) "
                            f"and ensure that the configuration is in a valid JSON format.\n\n"
                            f"Flair Helper supports legacy loading of YAML configurations, which will be automatically converted to JSON format. "
                            f"However, going forward, the JSON format is preferred and will be used for saving and processing the configuration.\n\n"
                            f"If you need assistance, please refer to the Flair Helper documentation or contact the bot maintainer."
                        )
                        if send_pm_on_wiki_config_update:
                            await subreddit.message(subject, message)
                        raise ValueError(f"Unsupported or invalid configuration format for /r/{subreddit.display_name}")

                cached_config = get_cached_config(subreddit.display_name)

                if cached_config is None or cached_config != updated_config:
                    # Check if the mod who edited the wiki page has the "config" permission
                    if updated_config[0]['GeneralConfiguration'].get('require_config_to_edit', False):
                        wiki_revision = await get_latest_wiki_revision(subreddit)
                        mod_name = wiki_revision['author']

                        if mod_name != bot_username:
                            mod_permissions = await check_mod_permissions(subreddit, mod_name)
                            if mod_permissions is not None and ('all' in mod_permissions or 'config' in mod_permissions):
                                # The moderator has the 'config' permission or 'all' permissions
                                pass
                            else:
                                # The moderator does not have the 'config' permission or is not a moderator
                                await error_handler(f"Mod {mod_name} does not have permission to edit wiki in /r/{subreddit.display_name}\n\nMod {mod_name} has the following permissions in /r/{subreddit.display_name}: {mod_permissions}", notify_discord=True)
                                break  # Skip reloading the configuration and continue with the next subreddit
                        # If mod_name is the bot's own username, proceed with caching the configuration

                    try:
                        await cache_config(subreddit.display_name, updated_config)
                        await error_handler(f"The Flair Helper wiki page configuration for /r/{subreddit.display_name} has been successfully cached and reloaded.", notify_discord=True)

                        if send_pm_on_wiki_config_update:
                            try:
                                subreddit_instance = await get_subreddit(reddit, subreddit.display_name)
                                await subreddit_instance.message(
                                    subject="Flair Helper Configuration Reloaded",
                                    message=f"The Flair Helper configuration for /r/{subreddit.display_name} has been successfully reloaded."
                                )
                            except asyncpraw.exceptions.RedditAPIException as e:
                                await error_handler(f"Error sending message to /r/{subreddit.display_name}: {e}", notify_discord=True)
                    except Exception as e:
                        await error_handler(f"Error caching configuration for /r/{subreddit.display_name}: {e}", notify_discord=True)
                        if send_pm_on_wiki_config_update:
                            try:
                                subreddit_instance = await get_subreddit(reddit, subreddit.display_name)
                                await subreddit_instance.message(
                                    subject="Flair Helper Configuration Error",
                                    message=f"The Flair Helper configuration for /r/{subreddit.display_name} could not be cached due to errors:\n\n{e}"
                                )
                            except asyncpraw.exceptions.RedditAPIException as e:
                                await error_handler(f"Error sending message to /r/{subreddit.display_name}: {e}", notify_discord=True)
                else:
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: The Flair Helper wiki page configuration for /r/{subreddit.display_name} has not changed.") if debugmode else None
                    await asyncio.sleep(1)  # Adjust the delay as needed
                break  # Configuration loaded successfully, exit the retry loop
            except (aiohttp.ClientError, asyncio.TimeoutError, asyncprawcore.exceptions.ServerError) as e:
                error_message = f"fetch_and_cache_configs: Error connecting to Reddit API or received 500 HTTP response: {str(e)}"
                print(error_message) if debugmode else None
                await error_handler(error_message, notify_discord=True)
                retries += 1
                if retries < max_retries:
                    print(f"Retrying in {retry_delay} seconds...")
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, max_retry_delay)
                else:
                    print(f"Max retries exceeded for /r/{subreddit.display_name}. Skipping...")
                    break  # Skip to the next subreddit if max retries exceeded
            except asyncpraw.exceptions.RedditAPIException as e:
                if "RATELIMIT" in str(e.message):
                    wait_time_match = re.search(r"for (\d+) minute", str(e.message))
                    if wait_time_match:
                        wait_minutes = int(wait_time_match.group(1))
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Rate limited. Waiting for {wait_minutes} minutes before retrying.")
                        await asyncio.sleep(wait_minutes * 60)
                        continue
                    else:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Rate limited, but could not extract wait time.")
                        await asyncio.sleep(60)  # Wait for a default duration before retrying
                        continue
                else:
                    await error_handler(f"fetch_and_cache_configs: Reddit API Exception in /r/{subreddit.display_name}: {e}", notify_discord=True)
                    break  # Skip to the next subreddit if a non-rate limit exception occurs
            except (asyncprawcore.exceptions.ResponseException, asyncprawcore.exceptions.RequestException) as e:
                await error_handler(f"Error loading configuration for /r/{subreddit.display_name}: {e}", notify_discord=True)
                retries += 1
                if retries < max_retries:
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Retrying in {retry_delay} seconds...") if debugmode else None
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, max_retry_delay)  # Double the retry delay, but don't exceed the maximum
                else:
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Max retries exceeded for /r/{subreddit.display_name}. Skipping...") if debugmode else None
                    break  # Skip to the next subreddit if max retries exceeded
            except asyncprawcore.exceptions.Forbidden:
                await error_handler(f"Error: Bot does not have permission to access the wiki page in /r/{subreddit.display_name}", notify_discord=True)
                break  # Skip retrying if the bot doesn't have permission
            except asyncprawcore.exceptions.NotFound:
                await error_handler(f"Flair Helper wiki page doesn't exist for /r/{subreddit.display_name}", notify_discord=True)
                if send_pm_on_wiki_config_update:
                    try:
                        subreddit_instance = await get_subreddit(reddit, subreddit.display_name)
                        await subreddit_instance.message(
                            subject="Flair Helper Wiki Page Not Found",
                            message=f"The Flair Helper wiki page doesn't exist for /r/{subreddit.display_name}. Please go to https://www.reddit.com/r/{subreddit.display_name}/wiki/flair_helper and create the page to add this subreddit.  You can send me a PM with 'list' or 'auto' to generate a sample configuration.\n\n[Generate a List of Flairs](https://www.reddit.com/message/compose?to=/u/{bot_username}&subject=list&message={subreddit.display_name})\n\n[Auto-Generate a sample Flair Helper Config](https://www.reddit.com/message/compose?to=/u/{bot_username}&subject=auto&message={subreddit.display_name})\n\nYou can find more information in the Flair Helper documentation on /r/Flair_Helper2/wiki/tutorial/ \n\nHappy Flairing!"
                        )
                    except asyncpraw.exceptions.RedditAPIException as e:
                        await error_handler(f"Error sending modmail to /r/{subreddit.display_name}: {e}", notify_discord=True)
                break  # Skip retrying if the wiki page doesn't exist

        await asyncio.sleep(retry_delay)  # Add a delay between subreddit configurations


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

async def update_usernotes(subreddit, author, note_text, link, mod_name, usernote_type_name=None):
    async with usernotes_lock:
        try:
            usernotes_wiki = await subreddit.wiki.get_page("usernotes")
            usernotes_content = usernotes_wiki.content_md
            usernotes_data = json.loads(usernotes_content)

            if 'blob' not in usernotes_data:
                usernotes_data['blob'] = ''

            decompressed_notes = decompress_notes(usernotes_data['blob'])

            if 'constants' not in usernotes_data:
                usernotes_data['constants'] = {'users': [], 'warnings': []}

            if mod_name not in usernotes_data['constants']['users']:
                usernotes_data['constants']['users'].append(mod_name)

            mod_index = usernotes_data['constants']['users'].index(mod_name)

            if usernote_type_name:
                if usernote_type_name not in usernotes_data['constants']['warnings']:
                    usernotes_data['constants']['warnings'].append(usernote_type_name)
                usernote_type_index = usernotes_data['constants']['warnings'].index(usernote_type_name)
            else:
                usernote_type_index = 0  # Use the default index if usernote_type_name is not provided

            add_usernote(decompressed_notes, author, note_text, link, mod_index, usernote_type_index)

            usernotes_data['blob'] = compress_notes(decompressed_notes)

            compressed_notes = json.dumps(usernotes_data)
            edit_reason = f"note added on user {author} via flair_helper2"
            await usernotes_wiki.edit(content=compressed_notes, reason=edit_reason)

        except Exception as e:
            await error_handler(f"update_usernotes: Error updating usernotes: {e}", notify_discord=True)


def add_usernote(notes, author, note_text, link, mod_index, usernote_type_index):
    if author not in notes:
        notes[author] = {"ns": []}

    timestamp = int(time.time())
    submission_id = link.split('/')[-3]
    new_note = {
        "n": f"[FH] {note_text}",
        "t": timestamp,
        "m": mod_index,
        "l": f"l,{submission_id}",
        "w": usernote_type_index
    }
    notes[author]["ns"].append(new_note)


async def send_modmail(subreddit, subject, message):
    await subreddit.message(subject, message)


def send_webhook_notification(config, post, flair_text, mod_name, flair_guid):
    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Sending webhook notification for flair GUID: {flair_guid}") if debugmode else None
    if 'webhook' in config[0]['GeneralConfiguration'] and any(flair['templateId'] == flair_guid and flair.get('sendToWebhook', False) for flair in config[1:]):
        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Webhook notification triggered for flair GUID: {flair_guid}") if debugmode else None

        webhook_url = config[0]['GeneralConfiguration']['webhook']
        webhook = DiscordWebhook(url=webhook_url)

        # Create the embed
        embed = DiscordEmbed(title=f"{post.title}", url="https://www.reddit.com"+post.permalink, description="Post Flaired: "+post.link_flair_text, color=242424)
        embed.add_embed_field(name="Author", value=post.author.name)
        embed.add_embed_field(name="Score", value=post.score)
        embed.add_embed_field(name="Created", value=datetime.utcfromtimestamp(post.created_utc).strftime('%b %u %Y %H:%M:%S UTC'))
        embed.add_embed_field(name="User Flair", value=flair_text)
        embed.add_embed_field(name="Subreddit", value="/r/"+post.subreddit.display_name)

        if not config[0]['GeneralConfiguration'].get('wh_exclude_mod', False):
            embed.add_embed_field(name="Actioned By", value=mod_name, inline=False)

        if not config[0]['GeneralConfiguration'].get('wh_exclude_reports', False):
            user_reports = []
            mod_reports = []

            for report in post.user_reports:
                user_reports.append(f"{report[0]} ({report[1]})")

            for report in post.mod_reports:
                if isinstance(report, list) and len(report) >= 2:
                    mod_reports.append(f"{report[1]} ({report[0]})")
                else:
                    mod_reports.append(str(report))

            if user_reports:
                user_reports_str = ", ".join(user_reports)
                embed.add_embed_field(name="User Reports", value=user_reports_str, inline=False)

            if mod_reports:
                mod_reports_str = ", ".join(mod_reports)
                embed.add_embed_field(name="Mod Reports", value=mod_reports_str, inline=False)

        if post.over_18 and not config[0]['GeneralConfiguration'].get('wh_include_nsfw_images', False):
            pass  # Exclude NSFW images unless explicitly included
        elif not config[0]['GeneralConfiguration'].get('wh_exclude_image', False):
            embed.set_image(url=post.url)

        # Add the embed to the webhook
        webhook.add_embed(embed)

        # Set the content if provided
        if 'wh_content' in config[0]['GeneralConfiguration']:
            webhook.set_content(config[0]['GeneralConfiguration']['wh_content'])

        # Send a ping if the score exceeds the specified threshold
        if 'wh_ping_over_score' in config[0]['GeneralConfiguration'] and 'wh_ping_over_ping' in config[0]['GeneralConfiguration']:
            wh_ping_over_score = config[0]['GeneralConfiguration']['wh_ping_over_score']
            if wh_ping_over_score is not None and post.score >= wh_ping_over_score:
                if config[0]['GeneralConfiguration']['wh_ping_over_ping'] == 'everyone':
                    webhook.set_content("@everyone")
                elif config[0]['GeneralConfiguration']['wh_ping_over_ping'] == 'here':
                    webhook.set_content("@here")
                else:
                    webhook.set_content(f"<@&{config[0]['GeneralConfiguration']['wh_ping_over_ping']}>")

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
async def process_flair_assignment(reddit, log_entry, config, subreddit, max_retries=3, retry_delay=5):
    target_fullname = log_entry.target_fullname
    if target_fullname.startswith('t3_'):  # Check if it's a submission

        for attempt in range(max_retries):
            try:
                submission_id = target_fullname[3:]  # Remove the 't3_' prefix
                post = await reddit.submission(submission_id)
                flair_guid = getattr(post, 'link_flair_template_id', None)  # Use getattr to safely retrieve the attribute
                # Get the post title and author for debugging
                post_author_name = post.author.name if post.author else "[deleted]"
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Flair GUID {flair_guid} detected on ID: {submission_id} on post '{post.title}' by {post_author_name} in /r/{subreddit.display_name}") if debugmode else None
                # boolean variable to track whether the author is deleted or suspended:
                is_author_deleted_or_suspended = post_author_name == "[deleted]"

                # Reload the configuration from the database
                config = get_cached_config(subreddit.display_name)
                break

            except asyncprawcore.exceptions.RequestException as e:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: process_flair_assignment: Error connecting to Reddit API: {str(e)}") if debugmode else None
                if attempt < max_retries - 1:
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Retrying in {retry_delay} seconds...") if debugmode else None
                    await asyncio.sleep(retry_delay)
                else:
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Max retries exceeded.") if debugmode else None
                    await error_handler(f"process_flair_assignment: Error processing flair for {post_author_name} in /r/{subreddit.display_name}: {e}", notify_discord=True)
                    # Submission detailed failed to low return from function.
                    return

        if config is None:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Configuration not found for /r/{subreddit.display_name}. Skipping flair assignment.")
            return

        if flair_guid and any(flair['templateId'] == flair_guid for flair in config[1:]):

            for attempt in range(max_retries):
                try:
                    # Retrieve the flair details from the configuration
                    flair_details = next(flair for flair in config[1:] if flair['templateId'] == flair_guid)

                    await post.load()
                    # Now that post data is loaded, ensure that author data is loaded
                    if post.author:
                        await post.author.load()
                        if hasattr(post.author, 'is_suspended') and post.author.is_suspended:
                            author_id = None
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Skipping author ID on ID: {submission_id} for suspended user: {post.author.name}") if debugmode else None
                        else:
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

                    # Only fetch the current flair if the author is not deleted or suspended
                    if not is_author_deleted_or_suspended:
                        current_flair = await fetch_user_flair(subreddit, post.author.name)  # Fetch the current flair asynchronously
                    else:
                        current_flair = None

                except asyncprawcore.exceptions.RequestException as e:
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: process_flair_assignment attempt: Error connecting to Reddit API: {str(e)}") if debugmode else None
                    if attempt < max_retries - 1:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Retrying in {retry_delay} seconds...") if debugmode else None
                        await asyncio.sleep(retry_delay)
                    else:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Max retries exceeded.") if debugmode else None
                        await error_handler(f"process_flair_assignment: Error processing flair for {post_author_name} in /r/{subreddit.display_name}: {e}", notify_discord=True)
                        # Submission detailed failed to low return from function.
                        return

            # Initialize defaults if the user has no current flair
            flair_text = ''
            flair_css_class = ''

            if current_flair:
                flair_text = current_flair.get('flair_text', '')
                flair_css_class = current_flair.get('flair_css_class', '')

            # Format the header, flair details, and footer with the placeholders
            formatted_header = config[0]['GeneralConfiguration']['header']
            formatted_footer = config[0]['GeneralConfiguration']['footer']
            skip_add_newlines = config[0]['GeneralConfiguration'].get('skip_add_newlines', False)
            require_config_to_edit = config[0]['GeneralConfiguration'].get('require_config_to_edit', False)
            ignore_same_flair_seconds = config[0]['GeneralConfiguration'].get('ignore_same_flair_seconds', 60)
            utc_offset = config[0]['GeneralConfiguration'].get('utc_offset', 0)
            custom_time_format = config[0]['GeneralConfiguration'].get('custom_time_format', '')

            if not skip_add_newlines:
                formatted_header += "\n\n"
                formatted_footer = "\n\n" + formatted_footer

            last_flair_time = getattr(post, '_last_flair_time', 0)
            if time.time() - last_flair_time < ignore_same_flair_seconds:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Ignoring same flair action on ID: {submission_id} within {ignore_same_flair_seconds} seconds") if debugmode else None
                return
            post._last_flair_time = time.time()

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
                formatted_footer = formatted_footer.replace(f"{{{{{placeholder}}}}}", str(value))

            # Replace placeholders in specific flair_details values
            formatted_flair_details = flair_details['notes']
            for placeholder, value in placeholders.items():
                formatted_flair_details = formatted_flair_details.replace(f"{{{{{placeholder}}}}}", str(value))

            removal_reason = f"{formatted_header}\n\n{formatted_flair_details}\n\n{formatted_footer}"

            # Execute the configured actions
            if 'approve' in flair_details and flair_details['approve']:
                for attempt in range(max_retries):
                    try:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Approve triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Submission approved on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                        await post.mod.approve()
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Submission unlocked on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                        await post.mod.unlock()
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Spoiler removed on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                        await post.mod.unspoiler()
                    except asyncprawcore.exceptions.RequestException as e:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: process_flair_assignment approve: Error connecting to Reddit API: {str(e)}") if debugmode else None
                        if attempt < max_retries - 1:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Retrying in {retry_delay} seconds...") if debugmode else None
                            await asyncio.sleep(retry_delay)
                        else:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Max retries exceeded. Skipping approve action...") if debugmode else None
                            await error_handler(f"process_flair_assignment: Error Approving Post ID: {submission_id} in /r/{subreddit.display_name}: {e}", notify_discord=True)
                    break

            if 'remove' in flair_details and flair_details['remove']:
                for attempt in range(max_retries):
                    try:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: remove triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                        mod_note = flair_details['usernote']['note'][:100] if 'usernote' in flair_details and flair_details['usernote']['enabled'] else ''

                        if flair_details.get('modlogReason'):
                            mod_note = flair_details['modlogReason'][:100]  # Truncate to 100 characters

                        await post.mod.remove(spam=False, mod_note=mod_note)
                    except asyncprawcore.exceptions.RequestException as e:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: process_flair_assignment remove: Error connecting to Reddit API: {str(e)}") if debugmode else None
                        if attempt < max_retries - 1:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Retrying in {retry_delay} seconds...") if debugmode else None
                            await asyncio.sleep(retry_delay)
                        else:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Max retries exceeded. Skipping remove action...") if debugmode else None
                            await error_handler(f"process_flair_assignment: Error Removing Post ID: {submission_id} in /r/{subreddit.display_name}: {e}", notify_discord=True)
                    break

            if not flair_details.get('remove') and flair_details.get('modlogReason'):
                for attempt in range(max_retries):
                    try:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: modlogReason triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                        await post.mod.create_note(note=flair_details['modlogReason'][:250])  # Truncate to 250 characters
                    except asyncprawcore.exceptions.RequestException as e:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: process_flair_assignment remove_modlogreason: Error connecting to Reddit API: {str(e)}") if debugmode else None
                        if attempt < max_retries - 1:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Retrying in {retry_delay} seconds...") if debugmode else None
                            await asyncio.sleep(retry_delay)
                        else:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Max retries exceeded. Skipping create_note action...") if debugmode else None
                            await error_handler(f"process_flair_assignment: Error Creating Mod Note for ID: {submission_id} in /r/{subreddit.display_name}: {e}", notify_discord=True)
                    break

            if 'comment' in flair_details and flair_details['comment']['enabled']:
                post_age_days = (datetime.utcnow() - datetime.utcfromtimestamp(post.created_utc)).days
                max_age = config[0]['GeneralConfiguration'].get('maxAgeForComment', 175)
                if post_age_days <= max_age:
                    comment_body = flair_details['comment'].get('body', '')
                    if comment_body.strip():
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: comment triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                        for attempt in range(max_retries):
                            try:
                                if flair_details['remove']:
                                    # If both 'remove' and 'comment' are configured for the flair GUID
                                    removal_type = config[0]['GeneralConfiguration'].get('removal_comment_type', '')
                                    if removal_type == '':
                                        removal_type = 'public_as_subreddit'  # Default to 'public' if removal_comment_type is blank or unset
                                    elif removal_type not in ['public', 'private', 'private_exposed', 'public_as_subreddit']:
                                        removal_type = 'public_as_subreddit'  # Use 'public' as the default if an invalid value is provided
                                    try:
                                        await post.mod.send_removal_message(message=removal_reason, type=removal_type)
                                    except asyncpraw.exceptions.RedditAPIException as e:
                                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Error sending removal message for post ID: {submission_id} in /r/{subreddit.display_name}: {e}")
                                else:
                                        # If only 'comment' is configured for the flair GUID
                                        comment = await post.reply(removal_reason)
                                        if flair_details['comment']['stickyComment']:
                                            await comment.mod.distinguish(sticky=True)
                                        if flair_details['comment']['lockComment']:
                                            await comment.mod.lock()
                            except asyncprawcore.exceptions.RequestException as e:
                                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: process_flair_assignment comment: Error connecting to Reddit API: {str(e)}") if debugmode else None
                                if attempt < max_retries - 1:
                                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Retrying in {retry_delay} seconds...") if debugmode else None
                                    await asyncio.sleep(retry_delay)
                                else:
                                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Max retries exceeded. Skipping comment action...") if debugmode else None
                                    await error_handler(f"process_flair_assignment: Error Commenting on Post ID: {submission_id} in /r/{subreddit.display_name}: {e}", notify_discord=True)
                                    #return # exit the process_flair_assignment function and proceed to next modlog
                            break # exit the try loop and proceed
                    else:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Skipping comment action due to empty comment body on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None

            if 'lock' in flair_details and flair_details['lock']:
                for attempt in range(max_retries):
                    try:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: lock triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                        await post.mod.lock()
                    except asyncprawcore.exceptions.RequestException as e:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: process_flair_assignment lock: Error connecting to Reddit API: {str(e)}") if debugmode else None
                        if attempt < max_retries - 1:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Retrying in {retry_delay} seconds...") if debugmode else None
                            await asyncio.sleep(retry_delay)
                        else:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Max retries exceeded. Skipping lock action...") if debugmode else None
                            await error_handler(f"process_flair_assignment: Error Locking Post ID: {submission_id} in /r/{subreddit.display_name}: {e}", notify_discord=True)
                    break

            if 'spoiler' in flair_details and flair_details['spoiler']:
                for attempt in range(max_retries):
                    try:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: spoiler triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                        await post.mod.spoiler()
                    except asyncprawcore.exceptions.RequestException as e:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: process_flair_assignment spoiler: Error connecting to Reddit API: {str(e)}") if debugmode else None
                        if attempt < max_retries - 1:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Retrying in {retry_delay} seconds...") if debugmode else None
                            await asyncio.sleep(retry_delay)
                        else:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Max retries exceeded. Skipping spoiler action...") if debugmode else None
                            await error_handler(f"process_flair_assignment: Error Spoilering Post ID: {submission_id} in /r/{subreddit.display_name}: {e}", notify_discord=True)
                    break

            if 'clearPostFlair' in flair_details and flair_details['clearPostFlair']:
                for attempt in range(max_retries):
                    try:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: remove_link_flair triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                        await post.mod.flair(text='', css_class='')
                    except asyncprawcore.exceptions.RequestException as e:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: process_flair_assignment clearPostFlair: Error connecting to Reddit API: {str(e)}") if debugmode else None
                        if attempt < max_retries - 1:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Retrying in {retry_delay} seconds...") if debugmode else None
                            await asyncio.sleep(retry_delay)
                        else:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Max retries exceeded. Skipping remove link flair action...") if debugmode else None
                            await error_handler(f"process_flair_assignment: Error Removing Link Flair on Post ID: {submission_id} in /r/{subreddit.display_name}: {e}", notify_discord=True)
                    break

            if 'sendToWebhook' in flair_details and flair_details['sendToWebhook']:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: send_to_webhook triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                # Send webhook notification
                send_webhook_notification(config, post, flair_text, log_entry.mod.name, flair_guid)


            # Only process the below if not suspended or deleted
            if not is_author_deleted_or_suspended:

                # Check if banning is configured for the flair GUID
                if 'ban' in flair_details and flair_details['ban']['enabled']:
                    ban_duration = flair_details['ban'].get('duration', '')
                    ban_message = flair_details['ban']['message']
                    ban_reason = flair_details['ban']['modNote']

                    if ban_message:
                        for placeholder, value in placeholders.items():
                            ban_message = ban_message.replace(f"{{{{{placeholder}}}}}", str(value))

                    if ban_reason:
                        for placeholder, value in placeholders.items():
                            ban_reason = ban_reason.replace(f"{{{{{placeholder}}}}}", str(value))[:100]

                    for attempt in range(max_retries):
                        try:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Debugging: ban_duration={ban_duration}, ban_message={ban_message}, ban_reason={ban_reason}") if debugmode else None
                            if ban_duration == '' or ban_duration is True:
                                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: permanent ban triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                                await subreddit.banned.add(post.author, ban_message=ban_message, ban_reason=ban_reason)
                            elif isinstance(ban_duration, int) and ban_duration > 0:
                                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Temporary ban triggered on ID: {submission_id} for {ban_duration} days in /r/{subreddit.display_name}") if debugmode else None
                                await subreddit.banned.add(post.author, ban_message=ban_message, ban_reason=ban_reason, duration=ban_duration)
                            else:
                                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Skipping ban action due to invalid ban duration on ID: {submission_id} for flair GUID: {flair_details['templateId']} in /r/{subreddit.display_name}") if debugmode else None
                        except asyncprawcore.exceptions.NotFound:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: User not found for banning on ID: {submission_id} in /r/{subreddit.display_name}")
                        except asyncprawcore.exceptions.RequestException as e:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: process_flair_assignment ban: Error connecting to Reddit API: {str(e)}") if debugmode else None
                            if attempt < max_retries - 1:
                                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Retrying in {retry_delay} seconds...") if debugmode else None
                                await asyncio.sleep(retry_delay)
                            else:
                                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Max retries exceeded. Skipping ban action...") if debugmode else None
                                await error_handler(f"process_flair_assignment: Error Banning User under Post ID: {submission_id} in /r/{subreddit.display_name}: {e}", notify_discord=True)
                        except Exception as e:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Error banning user on ID: {submission_id} in /r/{subreddit.display_name}: {e}")
                        break

                if 'unban' in flair_details and flair_details['unban']:
                    for attempt in range(max_retries):
                        try:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: unban triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                            await subreddit.banned.remove(post.author)
                        except asyncprawcore.exceptions.NotFound:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: User not found for unbanning on ID: {submission_id} in /r/{subreddit.display_name}")
                        except asyncprawcore.exceptions.RequestException as e:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: process_flair_assignment unban: Error connecting to Reddit API: {str(e)}") if debugmode else None
                            if attempt < max_retries - 1:
                                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Retrying in {retry_delay} seconds...") if debugmode else None
                                await asyncio.sleep(retry_delay)
                            else:
                                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Max retries exceeded. Skipping approve action...") if debugmode else None
                                await error_handler(f"process_flair_assignment: Error Unbanning user {post_author_name} in /r/{subreddit.display_name}: {e}", notify_discord=True)
                        except Exception as e:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Error unbanning user on ID: {submission_id} in /r/{subreddit.display_name}: {e}")
                        break


                if 'userFlair' in flair_details and flair_details['userFlair']['enabled']:
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: set_author_flair triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None

                    debug_current_Flair = f"{post_author_name} Current flair: text='{flair_text}', css_class='{flair_css_class}' |"
                    # Update the flair text based on the configuration
                    if flair_details['userFlair']['text']:
                        flair_text = flair_details['userFlair']['text']
                        for placeholder, value in placeholders.items():
                            flair_text = flair_text.replace(f"{{{{{placeholder}}}}}", str(value))
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Updating flair text to: '{flair_text}'") if verbosemode else None

                    # Update the flair CSS class based on the configuration
                    if flair_details['userFlair']['cssClass']:
                        flair_css_class = flair_details['userFlair']['cssClass']
                        for placeholder, value in placeholders.items():
                            flair_css_class = flair_css_class.replace(f"{{{{{placeholder}}}}}", str(value))
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Updating flair CSS class to: '{flair_css_class}'") if verbosemode else None

                    # Set the updated flair for the user
                    for attempt in range(max_retries):
                        try:
                            await subreddit.flair.set(post.author.name, text=flair_text, css_class=flair_css_class)
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: {debug_current_Flair} Updated to text='{flair_text}', css_class='{flair_css_class}'") if debugmode else None
                        except asyncprawcore.exceptions.NotFound:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: User not found for setting user flair on ID: {submission_id} in /r/{subreddit.display_name}")
                        except asyncprawcore.exceptions.RequestException as e:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: process_flair_assignment userFlair: Error connecting to Reddit API: {str(e)}")
                            if attempt < max_retries - 1:
                                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Retrying in {retry_delay} seconds...")
                                await asyncio.sleep(retry_delay)
                            else:
                                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Max retries exceeded. Skipping set_author_flair action...")
                                await error_handler(f"process_flair_assignment: Error updating flair for {post_author_name} in /r/{subreddit.display_name}: {e}", notify_discord=True)
                        except Exception as e:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Error setting user flair on ID: {submission_id} in /r/{subreddit.display_name}: {e}")
                        break



                if 'usernote' in flair_details and flair_details['usernote']['enabled']:
                    usernote_note = flair_details['usernote'].get('note', '')

                    if usernote_note.strip():
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: usernote triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                        author = post_author_name
                        note_text = flair_details['usernote']['note']
                        for placeholder, value in placeholders.items():
                            note_text = note_text.replace(f"{{{{{placeholder}}}}}", str(value))
                        link = post.permalink
                        mod_name = log_entry.mod.name
                        usernote_type_name = config[0]['GeneralConfiguration'].get('usernote_type_name', None)
                        for attempt in range(max_retries):
                            try:
                                await update_usernotes(subreddit, author, note_text, link, mod_name, usernote_type_name)
                            except Exception as e:
                                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Error adding usernote on ID: {submission_id} in /r/{subreddit.display_name}: {e}")
                            break
                    else:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Skipping usernote action due to empty usernote note on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None

                if 'contributor' in flair_details and flair_details['contributor']['enabled'] and flair_details['contributor']['action'] == 'add':
                    for attempt in range(max_retries):
                        try:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: add_contributor triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                            await subreddit.contributor.add(post.author)
                        except asyncprawcore.exceptions.NotFound:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: User not found for adding as contributor on ID: {submission_id} in /r/{subreddit.display_name}")
                        except asyncprawcore.exceptions.RequestException as e:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: process_flair_assignment usernote: Error connecting to Reddit API: {str(e)}") if debugmode else None
                            if attempt < max_retries - 1:
                                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Retrying in {retry_delay} seconds...") if debugmode else None
                                await asyncio.sleep(retry_delay)
                            else:
                                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Max retries exceeded. Skipping approve action...") if debugmode else None
                                await error_handler(f"process_flair_assignment: Error Approving Post ID: {submission_id} in /r/{subreddit.display_name}: {e}", notify_discord=True)
                        except Exception as e:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Error adding user as contributor on ID: {submission_id} in /r/{subreddit.display_name}: {e}")
                        break

                if 'contributor' in flair_details and flair_details['contributor']['enabled'] and flair_details['contributor']['action'] == 'remove':
                    for attempt in range(max_retries):
                        try:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: remove_contributor triggered on ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                            await subreddit.contributor.remove(post.author)
                        except asyncprawcore.exceptions.NotFound:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: User not found for removing as contributor on ID: {submission_id} in /r/{subreddit.display_name}")
                        except asyncprawcore.exceptions.RequestException as e:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: process_flair_assignment contributor: Error connecting to Reddit API: {str(e)}") if debugmode else None
                            if attempt < max_retries - 1:
                                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Retrying in {retry_delay} seconds...") if debugmode else None
                                await asyncio.sleep(retry_delay)
                            else:
                                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Max retries exceeded. Skipping approve action...") if debugmode else None
                                await error_handler(f"process_flair_assignment: Error Adding Contributor in /r/{subreddit.display_name}: {e}", notify_discord=True)
                        except Exception as e:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Error removing user as contributor on ID: {submission_id} in /r/{subreddit.display_name}: {e}")
                        break


            if flair_details.get('nukeUserComments', False):
                for attempt in range(max_retries):
                    try:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Nuking comments under Post ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None

                        # Fetch the comments of the submission
                        submission_comments = post.comments

                        # Nuke the comments
                        async for comment in submission_comments:
                            if not comment.removed and comment.distinguished != 'moderator':  # Check if the comment is not removed and not a moderator comment
                                for attempt in range(max_retries):
                                    try:
                                        await comment.mod.remove()
                                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Removed comment {comment.id} under Post ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None
                                    except asyncprawcore.exceptions.RequestException as e:
                                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: process_flair_assignment nukeUserComments: Error removing comment {str(e)}") if debugmode else None
                                        if attempt < max_retries - 1:
                                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Retrying in {retry_delay} seconds...") if debugmode else None
                                            await asyncio.sleep(retry_delay)
                                        else:
                                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Max retries exceeded. Skipping approve action...") if debugmode else None
                                            await error_handler(f"process_flair_assignment: Error Approving Post ID: {submission_id} in /r/{subreddit.display_name}: {e}", notify_discord=True)
                                    break

                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Finished nuking comments under Post ID: {submission_id} in /r/{subreddit.display_name}") if debugmode else None

                    except asyncprawcore.exceptions.RequestException as e:
                        error_message = f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: process_flair_assignment nukeUserComments: Error fetching comments for post ID: {submission_id} in /r/{subreddit.display_name}: {str(e)}"
                        print(error_message) if debugmode else None
                        await error_handler(error_message, notify_discord=True)
                        # Handle the error, e.g., retry or log the error
                    break



# Handle Private Messages to allow the bot to reply back with a list of flairs for convenience
async def handle_private_messages(reddit):
    async for message in reddit.inbox.unread(limit=None):
        if isinstance(message, asyncpraw.models.Message):

            if message.author == "reddit":
                # Ignore messages from the "reddit" user (admins or system messages)
                await message.mark_read()
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Skipping message from reddit") if debugmode else None
                continue

            if 'invitation to moderate' in message.subject.lower():
                if auto_accept_mod_invites:
                    subreddit = await get_subreddit(reddit, message.subreddit.display_name)
                    await subreddit.mod.accept_invite()
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Accepted mod invitation for r/{subreddit.display_name}") if debugmode else None
                    await discord_status_notification(f"Accepted mod invitation for r/{subreddit.display_name}")
                else:
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Received mod invitation for r/{message.subreddit.display_name} but auto-accept is disabled") if debugmode else None
                    await discord_status_notification(f"Received mod invitation for r/{message.subreddit.display_name} but auto-accept is disabled")

                await message.mark_read()  # Mark the mod invitation message as read
                continue  # Skip further processing for mod invitations

            else:
                body = message.body.strip()
                subreddit_name = body.split()[0]

                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: PM Received for {subreddit_name}") if debugmode else None

                if not re.match(r'^[a-zA-Z0-9_]{3,21}$', subreddit_name):
                    response = "Invalid subreddit name. The subreddit name must be between 3 and 21 characters long and can only contain letters, numbers, and underscores."
                else:
                    try:
                        subreddit = await get_subreddit(reddit, subreddit_name)
                        await subreddit.load()  # Load the subreddit data

                        if message.subject.lower() == 'list':
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: 'list' PM Received for {subreddit_name}") if debugmode else None
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

                        elif message.subject.lower() == 'auto':
                            try:
                                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: 'auto' PM Received for {subreddit_name}") if debugmode else None
                                if subreddit.user_is_moderator:  # Use the property directly

                                    response = await create_auto_flairhelper_wiki(reddit, subreddit, mode="pm")

                                else:
                                    response = f"You are not a moderator of /r/{subreddit_name}."
                            except asyncprawcore.exceptions.NotFound:
                                response = f"Subreddit /r/{subreddit_name} not found."

                        else:
                            response = "Unknown command. Available commands: 'list', 'auto'."

                    except asyncprawcore.exceptions.NotFound:
                        response = f"Subreddit /r/{subreddit_name} not found."

            try:
                await message.mark_read()
                if response:
                    await message.reply(response)
            except asyncprawcore.exceptions.Forbidden as e:
                if "USER_BLOCKED_MESSAGE" in str(e):
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Skipping reply to blocked user's message") if debugmode else None
                else:
                    await error_handler(f"handle_private_messages: Error replying to message (Forbidden): {e}", notify_discord=True)
            except asyncprawcore.exceptions.NotFound:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Message not found, skipping reply") if debugmode else None
            except Exception as e:
                await error_handler(f"handle_private_messages: Error replying to message: {e}", notify_discord=True)


async def create_auto_flairhelper_wiki(reddit, subreddit, mode):
    # Filter for mod-only flair templates
    flair_templates = [
        template async for template in subreddit.flair.link_templates
        if template['mod_only']
    ]

    general_config = {
        "GeneralConfiguration": {
            "notes": "\nThis is an Auto-Generated Configuration. Please review it carefully, all options are False by default to prevent an automatic configuration from causing troubles.\nPlease add additional settings as required, and enable what you wish.\n### You may also remove excess lines that you do not need, everything does not explicitly need to be defined as False\nIf something isn't set in this config, it won't be processed by default.",
            "header": "Hi /u/{{author}}, thanks for contributing to /r/{{subreddit}}. Unfortunately, your post was removed as it violates our rules:",
            "footer": "Please read the sidebar and the rules of our subreddit [here](https://www.reddit.com/r/{{subreddit}}/about/rules) before posting again. If you have any questions or concerns please [message the moderators through modmail](https://www.reddit.com/message/compose?to=/r/{{subreddit}}&subject=About my removed {{kind}}&message=I'm writing to you about the following {{kind}}: {{url}}. %0D%0DMy issue is...).",
            "usernote_type_name": "flair_helper_note",
            "removal_comment_type": "public_as_subreddit"
        }
    }

    json_config = [general_config]

    for template in flair_templates:
        flair_id = template['id']
        flair_text = template['text']

        flair_config = {
            "templateId": flair_id,
            "notes": f"{flair_text}",
            "approve": False,
            "remove": False,
            "lock": False,
            "spoiler": False,
            "clearPostFlair": False,
            "modlogReason": f"Violated Rule: {flair_text}",
            "comment": {
                "enabled": False,
                "body": f"Removed for violating rule: {flair_text}",
                "lockComment": False,
                "stickyComment": False,
                "distinguish": True,
                "headerFooter": True
            },
            "nukeUserComments": False,
            "usernote": {
                "enabled": False,
                "note": f"Removed: {flair_text}"
            },
            "contributor": {
                "enabled": False,
                "action": "add"
            },
            "userFlair": {
                "enabled": False,
                "text": "",
                "cssClass": ""
            },
            "ban": {
                "enabled": False,
                "duration": 0,
                "message": "",
                "modNote": ""
            },
            "unban": False,
            "sendToWebhook": False,
        }

        json_config.append(flair_config)

    json_output = json.dumps(json_config, indent=4)

    if mode == "pm":
        final_output = f"Here's a sample Flair Helper 2 configuration for /r/{subreddit.display_name} which you can place in [https://www.reddit.com/r/{subreddit.display_name}/wiki/flair_helper](https://www.reddit.com/r/{subreddit.display_name}/wiki/flair_helper)\n\n"
        final_output += "By default, all options are set to 'False' to prevent an automatic configuration from causing troubles. Please review the configuration carefully and enable the desired actions for each flair.\n\n"
        final_output += "\n```json\n" + json_output + "\n```"
        final_output += "\n\nPlease be sure to review all the detected flairs and remove any that may not be applicable (such as Mod Announcements, Notices, News, etc.)"

        # Implement the 10,000 character limit on the complete response for private messages
        while len(final_output) > 10000:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Response length > 10000 and is currently {len(final_output)}, removing extra entries") if debugmode else None

            # Remove the last flair config from the json_config list
            json_config.pop()

            # Regenerate the JSON output and response
            json_output = json.dumps(json_config, indent=4)
            final_output = f"Here's a sample Flair Helper 2 configuration for /r/{subreddit.display_name} which you can place in [https://www.reddit.com/r/{subreddit.display_name}/wiki/flair_helper](https://www.reddit.com/r/{subreddit.display_name}/wiki/flair_helper)\n\n"
            final_output += "By default, all options are set to 'False' to prevent an automatic configuration from causing troubles. Please review the configuration carefully and enable the desired actions for each flair.\n\n"
            final_output += "\n```json\n" + json_output + "\n```"
            final_output += "\n\nPlease be sure to review all the detected flairs and remove any that may not be applicable (such as Mod Announcements, Notices, News, etc.)"

    elif mode == "wiki":
        final_output = json_output

    print(f"\n\nFormatted JSON Output Message:\n\n{json_output}\n\n") if verbosemode else None

    return final_output


async def check_new_mod_invitations(reddit, bot_username):
    while True:
        current_subreddits = [sub async for sub in reddit.user.moderator_subreddits()]
        stored_subreddits = get_stored_subreddits()

        new_subreddits = [sub for sub in current_subreddits if sub.display_name not in stored_subreddits]

        for subreddit in new_subreddits:
            if f"u_{bot_username}" in subreddit.display_name:
                #print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: check_new_mod_invitations: Skipping bot's own user page: /r/{subreddit.display_name}") if debugmode else None
                continue  # Skip the bot's own user page

            subreddit_instance = await get_subreddit(reddit, subreddit.display_name)

            max_retries = 3
            retry_delay = 5  # Delay in seconds between retries

            for attempt in range(max_retries):
                try:
                    wiki_page = await subreddit.wiki.get_page('flair_helper')
                    wiki_content = wiki_page.content_md.strip()

                    if not wiki_content:
                        # Flair Helper wiki page exists but is blank
                        auto_gen_config = await create_auto_flairhelper_wiki(reddit, subreddit, mode="wiki")
                        await subreddit.wiki.create('flair_helper', auto_gen_config)
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Created auto_gen_config for 'flair_helper' wiki page for /r/{subreddit.display_name}") if debugmode else None

                        subject = f"Flair Helper Configuration Needed for /r/{subreddit.display_name}"
                        message = f"Hi! I noticed that I was recently added as a moderator to /r/{subreddit.display_name}.\n\nThe Flair Helper wiki page here: /r/{subreddit.display_name}/wiki/flair_helper exists but was currently blank.  I've went ahead and generated a working config based upon your 'Mod Only' flairs you have configured.  Otherwise, you can send me a PM with 'list' or 'auto' to generate a sample configuration.\n\n[Generate a List of Flairs](https://www.reddit.com/message/compose?to=/u/{bot_username}&subject=list&message={subreddit.display_name})\n\n[Auto-Generate a sample Flair Helper Config](https://www.reddit.com/message/compose?to=/u/{bot_username}&subject=auto&message={subreddit.display_name})\n\nYou can find more information in the Flair Helper documentation on /r/Flair_Helper2/wiki/tutorial/ \n\nHappy Flairing!"
                        await subreddit_instance.message(subject, message)
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Sent PM to /r/{subreddit.display_name} moderators to create a Flair Helper configuration (wiki page exists but is blank)") if debugmode else None
                    else:
                        # Flair Helper wiki page exists and has content
                        await fetch_and_cache_configs(reddit, bot_username, max_retries=3, retry_delay=5, single_sub=subreddit.display_name)
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Fetched and cached configuration for /r/{subreddit.display_name}") if debugmode else None
                    break

                except asyncprawcore.exceptions.NotFound:
                    # Flair Helper wiki page doesn't exist
                    auto_gen_config = await create_auto_flairhelper_wiki(reddit, subreddit, mode="wiki")
                    await subreddit.wiki.create('flair_helper', auto_gen_config)
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Created auto_gen_config for 'flair_helper' wiki page for /r/{subreddit.display_name}") if debugmode else None

                    subject = f"Flair Helper Configuration Needed for /r/{subreddit.display_name}"
                    message = f"Hi! I noticed that I was recently added as a moderator to /r/{subreddit.display_name}. To use my Flair Helper features, please setup your configuration on the newly created 'flair_helper' wiki page here: /r/{subreddit.display_name}/wiki/flair_helper \n\nI've went ahead and generated a working config based upon your 'Mod Only' flairs you have configured.  Otherwise, you can send me a PM with 'list' or 'auto' to generate a sample configuration.\n\n[Generate a List of Flairs](https://www.reddit.com/message/compose?to=/u/{bot_username}&subject=list&message={subreddit.display_name})\n\n[Auto-Generate a sample Flair Helper Config](https://www.reddit.com/message/compose?to=/u/{bot_username}&subject=auto&message={subreddit.display_name})\n\nYou can find more information in the Flair Helper documentation on /r/Flair_Helper2/wiki/tutorial/ \n\nHappy Flairing!"
                    await subreddit_instance.message(subject, message)
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Sent PM to /r/{subreddit.display_name} moderators to create a Flair Helper configuration (wiki page created)") if debugmode else None

                except asyncpraw.exceptions.RedditAPIException as e:
                    if "RATELIMIT" in str(e.message):
                        wait_time_match = re.search(r"for (\d+) minute", str(e.message))
                        if wait_time_match:
                            wait_minutes = int(wait_time_match.group(1))
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Rate limited. Waiting for {wait_minutes} minutes before retrying.") if debugmode else None
                            await discord_status_notification(f"check_new_mod_invitations Rate Limited for /r/{subreddit.display_name}.  Waiting for {wait_minutes} minutes before retrying.")
                            await asyncio.sleep(wait_minutes * 60 + retry_delay)
                            # After waiting, you might need to retry the operation that triggered the rate limit
                        else:
                            print("Rate limited, but could not extract wait time.") if debugmode else None
                            await discord_status_notification(f"check_new_mod_invitations Rate limited for /r/{subreddit.display_name}, but could not extract wait time.")
                            await asyncio.sleep(retry_delay)  # Wait for a default delay before retrying

                    else:
                        await error_handler(f"check_new_mod_invitations: Reddit API Exception in /r/{subreddit.display_name}: {e}", notify_discord=True)
                        traceback.print_exc()  # Print the full traceback
                        break


        await asyncio.sleep(3600)  # Check for new mod invitations every hour (adjust as needed)


# Primary Mod Log Monitor
async def monitor_mod_log(reddit, bot_username):
    accounts_to_ignore = ['AssistantBOT1', 'anyadditionalacctshere', 'thatmayinteractwithflair']

    #moderated_subreddits = []
    #async for subreddit in reddit.user.moderator_subreddits():
    #    moderated_subreddits.append(subreddit)
    #print(f"Moderated Subreddits: {moderated_subreddits}")

    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Flair Helper 2 has started up successfully!\nBot username: {bot_username}") if verbosemode else None
    await discord_status_notification(f"Flair Helper 2 has started up successfully!\nBot username: {bot_username}")

    while True:
        try:
            subreddit = await reddit.subreddit("mod")
            async for log_entry in subreddit.mod.stream.log(skip_existing=True):
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: New log entry: {log_entry.action}") if verbosemode else None

                if log_entry.action == 'wikirevise':
                    if 'flair_helper' in log_entry.details:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Flair Helper wiki page revised by {log_entry.mod} in /r/{log_entry.subreddit}") if debugmode else None
                        try:
                            await fetch_and_cache_configs(reddit, bot_username, max_retries=3, retry_delay=5, single_sub=log_entry.subreddit)  # Make sure fetch_and_cache_configs is async
                        except asyncprawcore.exceptions.NotFound:
                            error_output = f"monitor_mod_log: Flair Helper wiki page not found in /r/{log_entry.subreddit}"
                            print(error_output) if debugmode else None
                            errors_logger.error(error_output)
                elif (log_entry.action == 'editflair'
                    and log_entry.mod not in accounts_to_ignore
                    and log_entry.target_fullname is not None
                    and log_entry.target_fullname.startswith('t3_')):
                    # This is a link (submission) flair edit
                        # Get the post object
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Flair action detected by {log_entry.mod} in /r/{log_entry.subreddit}") if debugmode else None
                        subreddit_instance = await reddit.subreddit(log_entry.subreddit)
                        config = get_cached_config(log_entry.subreddit)
                        await process_flair_assignment(reddit, log_entry, config, subreddit_instance)  # Ensure process_flair_assignment is also async
                else:
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Ignoring action: {log_entry.action} in /r/{log_entry.subreddit}") if verbosemode else None

        except asyncprawcore.exceptions.ServerError as e:
            error_message = f"monitor_mod_log: Received 500 HTTP response: {str(e)}"
            print(error_message) if debugmode else None
            await error_handler(error_message, notify_discord=True)

            # Retry the request after a delay
            await asyncio.sleep(60)  # Wait for 60 seconds before retrying
            continue

        except asyncpraw.exceptions.RedditAPIException as e:
            if "RATELIMIT" in str(e.message):
                wait_time_match = re.search(r"for (\d+) minute", str(e.message))
                if wait_time_match:
                    wait_minutes = int(wait_time_match.group(1))
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Rate limited. Waiting for {wait_minutes} minutes before retrying.")
                    await asyncio.sleep(wait_minutes * 60)
                    # After waiting, you might need to retry the operation that triggered the rate limit
                    continue
                else:
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Rate limited, but could not extract wait time.")
                    await asyncio.sleep(60)  # Wait for a default duration before retrying
                    continue
            else:
                await error_handler(f"monitor_mod_log: Error: {e}", notify_discord=True)
                traceback.print_exc()  # Print the full traceback



# Create Multithreaded Instance to monitor all subs that have a valid Flair_Helper configuration
async def run_bot_async(reddit, bot_username):
    # Correctly await the asynchronous function call
    await fetch_and_cache_configs(reddit, bot_username)  # This is adapted to be async
    await monitor_mod_log(reddit, bot_username)


# Check for PM's every 60 seconds
async def monitor_private_messages(reddit):
    while True:
        await handle_private_messages(reddit)
        await asyncio.sleep(60)  # Sleep for 60 seconds before the next iteration


async def main():
    max_retries = 3
    retry_delay = 30  # Delay in seconds between retries
    max_retry_delay = 120  # Maximum delay in seconds between retries

    for attempt in range(max_retries):
        try:
            reddit = asyncpraw.Reddit("fh2_login")

            # Fetch the bot's username
            me = await reddit.user.me()
            bot_username = me.name

            # Create separate tasks for each coroutine
            bot_task = asyncio.create_task(run_bot_async(reddit, bot_username))
            pm_task = asyncio.create_task(monitor_private_messages(reddit))
            mod_invites_task = asyncio.create_task(check_new_mod_invitations(reddit, bot_username))

            await asyncio.gather(bot_task, pm_task, mod_invites_task)

            # If the bot starts successfully, break out of the retry loop
            break

        except (aiohttp.ClientConnectorError, asyncprawcore.exceptions.RequestException) as e:
            error_message = f"main(): Error connecting to Reddit API: {str(e)}"
            print(error_message)
            await error_handler(error_message, notify_discord=True)

            if attempt < max_retries - 1:
                retry_delay = min(retry_delay * 2, max_retry_delay)  # Exponential backoff
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Retrying in {retry_delay} seconds...")
                await asyncio.sleep(retry_delay)
            else:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Max retries exceeded. Exiting...")
                raise

if __name__ == "__main__":
    asyncio.run(main())
