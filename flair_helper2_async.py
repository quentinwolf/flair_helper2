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
from asyncprawcore import exceptions as asyncprawcore_exceptions
from asyncprawcore import ResponseException
from asyncprawcore import NotFound
from discord_webhook import DiscordWebhook, DiscordEmbed

import config  # Import your config.py

debugmode = config.debugmode
verbosemode = config.verbosemode

colored_console_output = config.colored_console_output

auto_accept_mod_invites = config.auto_accept_mod_invites

# Config Validation Errors are always PM'ed regardless of being True or False
send_pm_on_wiki_config_update = config.send_pm_on_wiki_config_update

discord_bot_notifications = config.discord_bot_notifications
discord_webhook_url = config.discord_webhook_url

logs_dir = config.logs_dir
if not os.path.exists(logs_dir):
    os.makedirs(logs_dir)

errors_filename = f'{logs_dir}errors.log'
logging.basicConfig(filename=errors_filename, level=logging.WARNING, format='%(asctime)s %(levelname)s: %(message)s', filemode='a')
errors_logger = logging.getLogger('errors')

logging.getLogger('aiohttp').setLevel(logging.CRITICAL)

usernotes_lock = asyncio.Lock()
database_lock = asyncio.Lock()

if colored_console_output:
    from termcolor import colored, cprint  # https://pypi.org/project/termcolor/


async def error_handler(error_message, notify_discord=False):
    print(error_message) if debugmode else None
    errors_logger.error(error_message)
    if notify_discord and discord_bot_notifications:
        await discord_status_notification(error_message)

# Error handler by u/ParkingPsychology https://www.reddit.com/r/redditdev/comments/xtrvb7/praw_how_to_handle/iqupaxz/
def reddit_error_handler(func):

    async def inner_function(*args, **kwargs):
        max_retries = 3
        retry_delay = 5
        max_retry_delay = 120

        for attempt in range(max_retries):
            try:
                return await func(*args, **kwargs)
            except asyncprawcore_exceptions.ServerError:
                sleep_ServerError = 240
                await error_handler(f"reddit_error_handler:\n Function: {func.__name__}\n Error: asyncprawcore.exceptions.ServerError\n Reddit may be down. Waiting {sleep_ServerError} seconds.", notify_discord=True)
                await asyncio.sleep(sleep_ServerError)
            except asyncprawcore_exceptions.Forbidden:
                sleep_Forbidden = 20
                await error_handler(f"reddit_error_handler:\n Function: {func.__name__}\n Error: asyncprawcore.exceptions.Forbidden\n Waiting {sleep_Forbidden} seconds.", notify_discord=True)
                await asyncio.sleep(sleep_Forbidden)
            except asyncprawcore_exceptions.TooManyRequests:
                sleep_TooManyRequests = 30
                await error_handler(f"reddit_error_handler:\n Function: {func.__name__}\n Error: asyncprawcore.exceptions.TooManyRequests\n Waiting {sleep_TooManyRequests} seconds.", notify_discord=True)
                await asyncio.sleep(sleep_TooManyRequests)
            except asyncprawcore_exceptions.ResponseException:
                sleep_ResponseException = 20
                await error_handler(f"reddit_error_handler:\n Function: {func.__name__}\n Error: asyncprawcore.exceptions.ResponseException\n Waiting {sleep_ResponseException} seconds.", notify_discord=True)
                await asyncio.sleep(sleep_ResponseException)
            except asyncprawcore_exceptions.RequestException:
                sleep_RequestException = 20
                await error_handler(f"reddit_error_handler:\n Function: {func.__name__}\n Error: asyncprawcore.exceptions.RequestException\n Waiting {sleep_RequestException} seconds.", notify_discord=True)
                await asyncio.sleep(sleep_RequestException)
            except asyncpraw.exceptions.RedditAPIException as exception:
                await error_handler(f"reddit_error_handler:\n Function: {func.__name__}\n Error: asyncpraw.exceptions.RedditAPIException", notify_discord=True)
                for subexception in exception.items:
                    if subexception.error_type == 'RATELIMIT':
                        message = subexception.message.replace("Looks like you've been doing that a lot. Take a break for ", "").replace("before trying again.", "")
                        if 'second' in message:
                            time_to_wait = int(message.split(" ")[0]) + 15
                            await error_handler(f"reddit_error_handler:\n Function: {func.__name__}\n Waiting for {time_to_wait} seconds due to rate limit", notify_discord=True)
                            await asyncio.sleep(time_to_wait)
                        elif 'minute' in message:
                            time_to_wait = (int(message.split(" ")[0]) * 60) + 15
                            await error_handler(f"reddit_error_handler:\n Function: {func.__name__}\n Waiting for {time_to_wait} seconds due to rate limit", notify_discord=True)
                            await asyncio.sleep(time_to_wait)
                    else:
                        await error_handler(f"reddit_error_handler:\n Function: {func.__name__}\n Different Error: {subexception}", notify_discord=True)
                await asyncio.sleep(retry_delay)
            except Exception as e:
                error_message = f"reddit_error_handler:\n Function: {func.__name__}\n Unexpected Error: {str(e)}\n called with\n Args: {args}\n kwargs: {kwargs}"
                print(error_message)
                print(traceback.format_exc())  # Print the traceback
                await error_handler(error_message, notify_discord=True)
            finally:
                print(f"Function: {func.__name__}\n called with\n Args: {args}\n kwargs: {kwargs}") if verbosemode else None

        # Retry loop
        for i in range(max_retries):
            if attempt < max_retries - 1:
                retry_delay = min(retry_delay * 2, max_retry_delay)  # Exponential backoff
                try:
                    return await inner_function(*args, **kwargs)
                except Exception as e:
                    await error_handler(f"reddit_error_handler\n Function: {func.__name__}\n Retry attempt {i+1} failed. Retrying in {retry_delay} seconds...\n Error: {str(e)}", notify_discord=True)
                    await asyncio.sleep(retry_delay)
            else:
                await error_handler(f"reddit_error_handler\n Function: {func.__name__}\n Max retries exceeded.", notify_discord=True)
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Function: {func.__name__} - Max retries exceeded. Exiting...") if debugmode else None
                raise RuntimeError("Max retries exceeded in reddit_error_handler") from None

    return inner_function



@reddit_error_handler
async def get_subreddit(reddit, subreddit_name):
    subreddit = await reddit.subreddit(subreddit_name)
    #subreddit_cache[subreddit_name] = subreddit
    #print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: get_subreddit: subreddit_name NOT in subreddit_cache: subreddit_name: {subreddit_name}, subreddit: {subreddit}") if debugmode else None
    return subreddit


# Create local sqlite db to cache/store Wiki Configs for all subs ones bot moderates
def create_configs_database():
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


def is_config_database_empty():
    conn = sqlite3.connect('flair_helper_configs.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='configs'")
    table_exists = c.fetchone()[0]
    if table_exists:
        c.execute("SELECT COUNT(*) FROM configs")
        count = c.fetchone()[0]
        conn.close()
        return count == 0
    else:
        conn.close()
        return True

@reddit_error_handler
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


def create_actions_database():
    conn = sqlite3.connect('flair_helper_actions.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS actions
                 (submission_id TEXT,
                  action TEXT,
                  completed INTEGER,
                  mod_name TEXT,
                  flair_guid TEXT)''')
    conn.commit()
    conn.close()

def insert_actions_to_database(submission_id, actions, mod_name, flair_guid):
    conn = sqlite3.connect('flair_helper_actions.db')
    c = conn.cursor()
    for action in actions:
        c.execute("INSERT INTO actions VALUES (?, ?, ?, ?, ?)", (submission_id, action, 0, mod_name, flair_guid))
    conn.commit()
    conn.close()

def get_pending_submission_ids_from_database():
    conn = sqlite3.connect('flair_helper_actions.db')
    c = conn.cursor()
    c.execute("SELECT DISTINCT submission_id, mod_name FROM actions WHERE completed = 0")
    pending_submission_ids = c.fetchall()
    conn.close()
    return pending_submission_ids

def mark_action_as_completed(submission_id, action):
    conn = sqlite3.connect('flair_helper_actions.db')
    c = conn.cursor()
    c.execute("UPDATE actions SET completed = 1 WHERE submission_id = ? AND action = ?", (submission_id, action))
    conn.commit()
    conn.close()

def is_action_completed(submission_id, action):
    conn = sqlite3.connect('flair_helper_actions.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM actions WHERE submission_id = ? AND action = ? AND completed = 1", (submission_id, action))
    completed_count = c.fetchone()[0]
    conn.close()
    return completed_count > 0

def is_submission_completed(submission_id):
    conn = sqlite3.connect('flair_helper_actions.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM actions WHERE submission_id = ? AND completed = 0", (submission_id,))
    pending_count = c.fetchone()[0]
    conn.close()
    return pending_count == 0

def delete_completed_actions(submission_id):
    conn = sqlite3.connect('flair_helper_actions.db')
    c = conn.cursor()
    c.execute("DELETE FROM actions WHERE submission_id = ? AND completed = 1", (submission_id,))
    conn.commit()
    conn.close()


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
                "enabled": flair_id in yaml_config.get('set_author_flair_text', {}) or
                           flair_id in yaml_config.get('set_author_flair_css_class', {}) or
                           flair_id in yaml_config.get('set_author_flair_template_id', {}),
                "text": yaml_config.get('set_author_flair_text', {}).get(flair_id, ''),
                "cssClass": yaml_config.get('set_author_flair_css_class', {}).get(flair_id, ''),
                "templateId": yaml_config.get('set_author_flair_template_id', {}).get(flair_id, '')
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


@reddit_error_handler
async def check_mod_permissions(subreddit, mod_name):
    moderators = await subreddit.moderator()
    for moderator in moderators:
        if moderator.name == mod_name:
            mod_permissions = set(moderator.mod_permissions)
            #print(f"Debugging: Mod {mod_name} has the following permissions in /r/{subreddit.display_name}: {mod_permissions}") if debugmode else None
            return mod_permissions
    #print(f"Debugging: Mod {mod_name} is not a moderator of /r/{subreddit.display_name}") if debugmode else None
    return None


def correct_config(config):
    corrected_config = []

    for item in config:
        if isinstance(item, dict):
            corrected_item = {}
            for key, value in item.items():
                if isinstance(value, str):
                    # Replace newline characters with '\n'
                    #value = value.replace("\n", "\\n")
                    value = value.replace("\\n", "\n")

                corrected_item[key] = value
            corrected_config.append(corrected_item)
        else:
            corrected_config.append(item)

    return corrected_config


@reddit_error_handler
async def fetch_and_cache_configs(reddit, bot_username, max_retries=3, retry_delay=1, max_retry_delay=60, single_sub=None):
    delay_between_wiki_fetch = 1
    create_configs_database()
    moderated_subreddits = []
    if single_sub:
        moderated_subreddits.append(await get_subreddit(reddit, single_sub))
    else:
        async for subreddit in reddit.user.moderator_subreddits():
            moderated_subreddits.append(subreddit)

    semaphore = asyncio.Semaphore(3)  # Limit the number of concurrent tasks to 4

    tasks = []
    for subreddit in moderated_subreddits:
        if f"u_{bot_username}" in subreddit.display_name:
            continue  # Skip the bot's own user page

        async with semaphore:
            tasks.append(process_subreddit_config(reddit, subreddit, bot_username, max_retries, retry_delay, max_retry_delay, delay_between_wiki_fetch))

    await asyncio.gather(*tasks)
    if single_sub:
        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Completed checking Wiki page configuration for {subreddit.display_name}.") if debugmode else None
    else:
        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Completed checking all Wiki page configuration.") if debugmode else None


async def process_subreddit_config(reddit, subreddit, bot_username, max_retries, retry_delay, max_retry_delay, delay_between_wiki_fetch):
    retries = 0

    if colored_console_output:
        disp_subreddit_displayname = colored("/r/"+subreddit.display_name, "cyan", attrs=["underline"])
    else:
        disp_subreddit_displayname = subreddit.display_name

    while retries < max_retries:
        wiki_content = ""  # Initialize wiki_content with a default value
        try:
            # Access wiki page using asyncpraw
            wiki_page = await subreddit.wiki.get_page('flair_helper')
            wiki_content = wiki_page.content_md.strip()
            # The rest of your code to handle the wiki content goes here
        except Exception as e:
            # Handle exceptions appropriately
            await error_handler(f"Error accessing the {subreddit.display_name} flair_helper wiki page: {e}", notify_discord=True)

        if not wiki_content:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Flair Helper configuration for {disp_subreddit_displayname} is blank. Skipping...") if debugmode else None
            break  # Skip processing if the wiki page is blank



        if wiki_content.strip().startswith('['):
            # Content starts with '[', assume it's JSON
            try:
                updated_config = json.loads(wiki_content)
                print(f"Configuration for {subreddit.display_name} is in JSON format.") if verbosemode else None
            except json.JSONDecodeError as e:
                await error_handler(f"Invalid JSON format for {subreddit.display_name}. Error details: {str(e)}", notify_discord=True)

                wiki_revision = await get_latest_wiki_revision(subreddit)
                mod_name = wiki_revision['author']
                mod_name_str = str(mod_name)

                # If both JSON and YAML parsing fail, send a notification to the subreddit and the mod who made the edit
                subject = f"Flair Helper Configuration Error in /r/{subreddit.display_name}"
                message = (
                    f"The [Flair Helper configuration](https://www.reddit.com/r/{subreddit.display_name}/wiki/edit/flair_helper) for /r/{subreddit.display_name} is in an unsupported or invalid format.\n\n"
                    f"Please check the [flair_helper wiki page](https://www.reddit.com/r/{subreddit.display_name}/wiki/edit/flair_helper) and ensure that the configuration is in a valid JSON or YAML format.\n\n"
                    f"-----\n\nError details: {str(e)}\n\n-----\n\n"
                    f"Flair Helper will continue using the previously cached configuration until the format is fixed.\n\n"
                    f"You may wish to try running your config through [JSONLint](https://jsonlint.com) for JSON or [YAMLLint](http://www.yamllint.com/) for YAML to validate and find any errors first."
                )
                try:
                    # Message the Subreddit
                    #subreddit_instance = await reddit.subreddit(subreddit.display_name)
                    #await subreddit_instance.message(subject, message)

                    # Message the Moderator who made the change
                    redditor = await reddit.redditor(mod_name_str)
                    await redditor.message(subject, message)
                except Exception as e:
                    await error_handler(f"Error sending message to {subreddit.display_name} or moderator {mod_name}: {str(e)}", notify_discord=True)

                return
        else:
            # Content doesn't start with '[', assume it's YAML
            try:
                updated_config = yaml.safe_load(wiki_content)
                await error_handler(f"Configuration for {subreddit.display_name} is in YAML format. Converting to JSON.", notify_discord=True)
                updated_config = convert_yaml_to_json(updated_config)
            except yaml.YAMLError as e:
                await error_handler(f"Invalid YAML format for {subreddit.display_name}. Error details: {str(e)}", notify_discord=True)

                wiki_revision = await get_latest_wiki_revision(subreddit)
                mod_name = wiki_revision['author']
                mod_name_str = str(mod_name)

                # If both JSON and YAML parsing fail, send a notification to the subreddit and the mod who made the edit
                subject = f"Flair Helper Configuration Error in /r/{subreddit.display_name}"
                message = (
                    f"The [Flair Helper configuration](https://www.reddit.com/r/{subreddit.display_name}/wiki/edit/flair_helper) for /r/{subreddit.display_name} is in an unsupported or invalid format.\n\n"
                    f"Please check the [flair_helper wiki page](https://www.reddit.com/r/{subreddit.display_name}/wiki/edit/flair_helper) and ensure that the configuration is in a valid JSON or YAML format.\n\n"
                    f"-----\n\nError details: {str(e)}\n\n-----\n\n"
                    f"Flair Helper will continue using the previously cached configuration until the format is fixed.\n\n"
                    f"You may wish to try running your config through [JSONLint](https://jsonlint.com) for JSON or [YAMLLint](http://www.yamllint.com/) for YAML to validate and find any errors first."
                )
                try:
                    # Message the Subreddit
                    #subreddit_instance = await reddit.subreddit(subreddit.display_name)
                    #await subreddit_instance.message(subject, message)

                    # Message the Moderator who made the change
                    redditor = await reddit.redditor(mod_name_str)
                    await redditor.message(subject, message)
                except Exception as e:
                    await error_handler(f"Error sending message to {subreddit.display_name} or moderator {mod_name}: {str(e)}", notify_discord=True)

                return



        # Perform validation and automatic correction
        updated_config = correct_config(updated_config)

        cached_config = get_cached_config(subreddit.display_name)

        if cached_config is None or cached_config != updated_config:
            # Check if the mod who edited the wiki page has the "config" permission
            wiki_revision = await get_latest_wiki_revision(subreddit)
            mod_name = wiki_revision['author']

            if updated_config[0]['GeneralConfiguration'].get('require_config_to_edit', False):
                if mod_name != bot_username:
                    mod_permissions = await check_mod_permissions(subreddit, mod_name)
                    if mod_permissions is not None and ('all' in mod_permissions or 'config' in mod_permissions):
                        # The moderator has the 'config' permission or 'all' permissions
                        pass
                    else:
                        # The moderator does not have the 'config' permission or is not a moderator
                        await error_handler(f"Mod {mod_name} does not have permission to edit wiki in {subreddit.display_name}\n\nMod {mod_name} has the following permissions in {subreddit.display_name}: {mod_permissions}", notify_discord=True)
                        break  # Skip reloading the configuration and continue with the next subreddit
                # If mod_name is the bot's own username, proceed with caching the configuration

            try:
                await cache_config(subreddit.display_name, updated_config)
                await error_handler(f"The [Flair Helper wiki page configuration](https://www.reddit.com/r/{subreddit.display_name}/wiki/edit/flair_helper) for {subreddit.display_name} has been successfully cached and reloaded.", notify_discord=False)

                # Save the validated and corrected configuration back to the wiki page
                await wiki_page.edit(content=json.dumps(updated_config, indent=4))

                if send_pm_on_wiki_config_update:
                    try:
                        subreddit_instance = await get_subreddit(reddit, subreddit.display_name)
                        await subreddit_instance.message(
                            subject="Flair Helper Configuration Reloaded",
                            message=f"Changes made by {mod_name} to the [Flair Helper configuration](https://www.reddit.com/r/{subreddit.display_name}/wiki/edit/flair_helper) for /r/{subreddit.display_name} has been successfully reloaded."
                        )
                    except asyncpraw.exceptions.RedditAPIException as e:
                        await error_handler(f"Error sending message to {subreddit.display_name}: {e}", notify_discord=True)
            except Exception as e:
                await error_handler(f"Error caching configuration for {subreddit.display_name}: {e}", notify_discord=True)
                if send_pm_on_wiki_config_update:
                    try:
                        subreddit_instance = await get_subreddit(reddit, subreddit.display_name)
                        await subreddit_instance.message(
                            subject="Flair Helper Configuration Error",
                            message=f"The Flair Helper configuration for /r/{subreddit.display_name} could not be cached due to errors:\n\n{e}"
                        )
                    except asyncpraw.exceptions.RedditAPIException as e:
                        await error_handler(f"Error sending message to {subreddit.display_name}: {e}", notify_discord=True)
        else:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: The Flair Helper wiki page configuration for {disp_subreddit_displayname} has not changed.") if debugmode else None
            #await asyncio.sleep(1)  # Adjust the delay as needed
        break  # Configuration loaded successfully, exit the retry loop

    await asyncio.sleep(delay_between_wiki_fetch)  # Add a delay between subreddit configurations

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

@reddit_error_handler
async def update_usernotes(subreddit, author, note_text, link, mod_name, usernote_type_name=None, max_retries=3, retry_delay=5):
    async with usernotes_lock:
        for attempt in range(max_retries):
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
                break  # Exit the retry loop if the update is successful
            except Exception as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                else:
                    error_message = f"Failed to update usernotes for user {author}"
                    print(error_message) if debugmode else None
                    raise RuntimeError(error_message) from e



def send_webhook_notification(config, post, flair_text, mod_name, flair_guid):
    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Sending webhook notification for flair GUID: {flair_guid}") if debugmode else None
    if 'webhook' in config[0]['GeneralConfiguration'] and any(flair['templateId'] == flair_guid and flair.get('sendToWebhook', False) for flair in config[1:]):
        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Webhook notification triggered for flair GUID: {flair_guid}") if debugmode else None

        webhook_url = config[0]['GeneralConfiguration']['webhook']
        webhook = DiscordWebhook(url=webhook_url)

        post_author_name = post.author.name if post.author else "[deleted]"

        # Create the embed
        embed = DiscordEmbed(title=f"{post.title}", url="https://www.reddit.com"+post.permalink, description="Post Flaired: "+post.link_flair_text, color=242424)
        embed.add_embed_field(name="Author", value=post_author_name)
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
@reddit_error_handler
async def fetch_user_flair(subreddit, username):
    async for flair in subreddit.flair(redditor=username):
        #print(f"Flair: {flair}") if debugmode else None
        return flair  # Return the first (and presumably only) flair setting
    #print(f"flair: None") if debugmode else None
    return None  # If no flair is set

# Primary process to handle any flair changes that appear in the logs
@reddit_error_handler
async def process_flair_assignment(reddit, post, config, subreddit, mod_name, max_retries=3, retry_delay=5):

    #submission_id = target_fullname[3:]  # Remove the 't3_' prefix
    #post = await reddit.submission(submission_id)
    flair_guid = getattr(post, 'link_flair_template_id', None)  # Use getattr to safely retrieve the attribute
    flair_details = next((flair for flair in config[1:] if flair['templateId'] == flair_guid), None)

    if colored_console_output:
        disp_subreddit_displayname = colored("/r/"+subreddit.display_name, "cyan", attrs=["underline"])
        disp_submission_id = colored(post.id, "yellow")
        disp_flair_guid = colored(flair_guid, "magenta")
    else:
        disp_subreddit_displayname = subreddit.display_name
        disp_submission_id = post.id
        disp_flair_guid = flair_guid

    if flair_details is None:
        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Flair GUID {disp_flair_guid} not found in the configuration for {disp_subreddit_displayname}") if debugmode else None
        return

    submission_id = post.id

    # Get the post title and author for debugging
    post_author_name = post.author.name if post.author else "[deleted]"
    # boolean variable to track whether the author is deleted or suspended:
    is_author_deleted_or_suspended = post_author_name == "[deleted]"

    # Reload the configuration from the database
    config = get_cached_config(subreddit.display_name)


    if config is None:
        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Configuration not found for {disp_subreddit_displayname}. Skipping flair assignment.")
        return

    if flair_guid and any(flair['templateId'] == flair_guid for flair in config[1:]):
        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Flair GUID {disp_flair_guid} detected on ID: {disp_submission_id} on post '{post.title}' by {post_author_name} in {disp_subreddit_displayname}") if debugmode else None

        # Get the "notes" field for the current flair GUID
        flair_notes = next((flair['notes'] for flair in config[1:] if flair['templateId'] == flair_guid), None)
        if flair_notes:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: || Flair Notes || {flair_notes}") if debugmode else None
        #else:
        #    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: No flair notes found for GUID {disp_flair_guid}") if debugmode else None


        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Flair details: {flair_details}") if verbosemode else None

        try:
            await post.load()
            # Now that post data is loaded, ensure that author data is loaded
            if post.author:
                await post.author.load()
                if hasattr(post.author, 'is_suspended') and post.author.is_suspended:
                    author_id = None
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Skipping author ID on ID: {disp_submission_id} for suspended user: {post.author.name}") if debugmode else None
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

            print(f"User Info... \nis_author_deleted_or_suspended: {is_author_deleted_or_suspended}\npost.author: {post.author}\nauthor_id: {author_id}") if verbosemode else None

        except (asyncprawcore.exceptions.NotFound, asyncprawcore.exceptions.Forbidden) as e:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Error loading post or author data for ID: {disp_submission_id}. Post may be removed or author may be shadowbanned/deleted. Skipping flair assignment.") if debugmode else None
            mark_action_as_completed(submission_id, 'comment')
            mark_action_as_completed(submission_id, 'approve')
            mark_action_as_completed(submission_id, 'remove')
            mark_action_as_completed(submission_id, 'lock')
            mark_action_as_completed(submission_id, 'modlogReason')
            mark_action_as_completed(submission_id, 'ban')
            mark_action_as_completed(submission_id, 'unban')
            mark_action_as_completed(submission_id, 'userFlair')
            mark_action_as_completed(submission_id, 'usernote')
            mark_action_as_completed(submission_id, 'contributor')
            mark_action_as_completed(submission_id, 'sendToWebhook')
            await asyncio.sleep(2)
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
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Ignoring same flair action on ID: {disp_submission_id} within {ignore_same_flair_seconds} seconds") if debugmode else None
            return
        post._last_flair_time = time.time()

        now = datetime.utcnow() + timedelta(hours=utc_offset)
        created_time = datetime.utcfromtimestamp(post.created_utc) + timedelta(hours=utc_offset)

        placeholders = {
            'time_unix': int(now.timestamp()),
            'time_iso': now.isoformat(),
            'time_custom': now.strftime(custom_time_format) if custom_time_format else '',
            'created_unix': int(created_time.timestamp()),
            'created_iso': created_time.isoformat(),
            'created_custom': created_time.strftime(custom_time_format) if custom_time_format else ''
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
            'mod': mod_name,
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
        formatted_flair_removal_details = flair_details['comment'].get('body', '')
        for placeholder, value in placeholders.items():
            formatted_flair_removal_details = formatted_flair_removal_details.replace(f"{{{{{placeholder}}}}}", str(value))
        formatted_removal_reason_comment = f"{formatted_header}\n\n{formatted_flair_removal_details}\n\n{formatted_footer}"

        # Execute the configured actions
        if not is_action_completed(submission_id, 'approve') and 'approve' in flair_details and flair_details['approve']:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - Approve triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None

            await post.load()

            if post.removed:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - Submission approved on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
                await post.mod.approve()
            else:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - Submission already approved on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None

            if post.locked:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - Submission unlocked on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
                await post.mod.unlock()
            else:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - Submission already unlocked on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None

            if post.spoiler:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - Spoiler removed on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
                await post.mod.unspoiler()
            else:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - Submission not spoilered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None

            mark_action_as_completed(submission_id, 'approve')

        if not is_action_completed(submission_id, 'remove') and 'remove' in flair_details and flair_details['remove']:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - remove triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None

            if post.removed:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Post {disp_submission_id} is already removed. Marking action as completed.") if debugmode else None
                mark_action_as_completed(submission_id, 'remove')
                mark_action_as_completed(submission_id, 'modlogReason')
            else:
                mod_note = flair_details['usernote']['note'][:100] if 'usernote' in flair_details and flair_details['usernote']['enabled'] else ''

                if flair_details.get('modlogReason'):
                    mod_note = flair_details['modlogReason'][:100]  # Truncate to 100 characters

                await post.mod.remove(spam=False, mod_note=mod_note)
                mark_action_as_completed(submission_id, 'remove')
                mark_action_as_completed(submission_id, 'modlogReason')

        if not flair_details.get('remove') and not is_action_completed(submission_id, 'modlogReason') and flair_details.get('modlogReason'):
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - modlogReason triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
            await post.mod.create_note(note=flair_details['modlogReason'][:250])  # Truncate to 250 characters
            mark_action_as_completed(submission_id, 'modlogReason')

        if not is_action_completed(submission_id, 'lock') and 'lock' in flair_details and flair_details['lock']:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - lock triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None

            if post.locked:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Post {disp_submission_id} is already locked. Marking action as completed.") if debugmode else None
                mark_action_as_completed(submission_id, 'lock')
            else:
                await post.mod.lock()
                mark_action_as_completed(submission_id, 'lock')

        if not is_action_completed(submission_id, 'spoiler') and 'spoiler' in flair_details and flair_details['spoiler']:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - spoiler triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None

            if post.spoiler:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Post {disp_submission_id} is already spoilered. Marking action as completed.") if debugmode else None
                mark_action_as_completed(submission_id, 'spoiler')
            else:
                await post.mod.spoiler()
                mark_action_as_completed(submission_id, 'spoiler')

        if not is_action_completed(submission_id, 'clearPostFlair') and 'clearPostFlair' in flair_details and flair_details['clearPostFlair']:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - remove_link_flair triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
            await post.mod.flair(text='', css_class='')
            mark_action_as_completed(submission_id, 'clearPostFlair')

        if not is_action_completed(submission_id, 'sendToWebhook') and 'sendToWebhook' in flair_details and flair_details['sendToWebhook']:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - send_to_webhook triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
            # Send webhook notification
            send_webhook_notification(config, post, flair_text, mod_name, flair_guid)
            mark_action_as_completed(submission_id, 'sendToWebhook')


        # Only process the below if not suspended or deleted
        if not is_author_deleted_or_suspended:

            if not is_action_completed(submission_id, 'comment') and 'comment' in flair_details and flair_details['comment']['enabled']:
                post_age_days = (datetime.utcnow() - datetime.utcfromtimestamp(post.created_utc)).days
                max_age = config[0]['GeneralConfiguration'].get('maxAgeForComment', 175)
                if post_age_days <= max_age:
                    comment_body = flair_details['comment'].get('body', '')
                    if comment_body.strip():
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - comment triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
                        if flair_details['remove']:
                            # If both 'remove' and 'comment' are configured for the flair GUID
                            removal_type = config[0]['GeneralConfiguration'].get('removal_comment_type', '')
                            if removal_type == '':
                                removal_type = 'public_as_subreddit'  # Default to 'public' if removal_comment_type is blank or unset
                            elif removal_type not in ['public', 'private', 'private_exposed', 'public_as_subreddit']:
                                removal_type = 'public_as_subreddit'  # Use 'public' as the default if an invalid value is provided
                            await post.mod.send_removal_message(message=formatted_removal_reason_comment, type=removal_type)
                        else:
                            # If only 'comment' is configured for the flair GUID
                            comment = await post.reply(formatted_removal_reason_comment)
                            if flair_details['comment']['stickyComment']:
                                await comment.mod.distinguish(sticky=True)
                            if flair_details['comment']['lockComment']:
                                await comment.mod.lock()
                        mark_action_as_completed(submission_id, 'comment')
                    else:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Skipping comment action due to empty comment body on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
                        mark_action_as_completed(submission_id, 'comment')
                else:
                    # Submission over age, mark as completed.
                    mark_action_as_completed(submission_id, 'comment')

            # Check if banning is configured for the flair GUID
            if not is_action_completed(submission_id, 'ban') and 'ban' in flair_details and flair_details['ban']['enabled']:
                ban_duration = flair_details['ban'].get('duration', '')
                ban_message = flair_details['ban']['message']
                ban_reason = flair_details['ban']['modNote']

                if ban_message:
                    for placeholder, value in placeholders.items():
                        ban_message = ban_message.replace(f"{{{{{placeholder}}}}}", str(value))

                if ban_reason:
                    for placeholder, value in placeholders.items():
                        ban_reason = ban_reason.replace(f"{{{{{placeholder}}}}}", str(value))[:100]

                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Debugging: ban_duration={ban_duration}, ban_message={ban_message}, ban_reason={ban_reason}") if debugmode else None
                if ban_duration == '' or ban_duration is True:
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - permanent ban triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
                    await subreddit.banned.add(post.author, ban_message=ban_message, ban_reason=ban_reason)
                    mark_action_as_completed(submission_id, 'ban')
                elif isinstance(ban_duration, int) and ban_duration > 0:
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - temporary ban triggered on ID: {disp_submission_id} for {ban_duration} days in {disp_subreddit_displayname}") if debugmode else None
                    await subreddit.banned.add(post.author, ban_message=ban_message, ban_reason=ban_reason, duration=ban_duration)
                    mark_action_as_completed(submission_id, 'ban')
                else:
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Skipping ban action due to invalid ban duration on ID: {disp_submission_id} for flair GUID: {flair_details['templateId']} in {disp_subreddit_displayname}") if debugmode else None
                    try:
                        subreddit_instance = await get_subreddit(reddit, subreddit.display_name)
                        await subreddit.message(
                            subject="Invalid Configuration",
                            message=f"The Ban Action for [this submission](https://redd.it/{post.id}) under /r/{subreddit.display_name} was not applied for user /u/{post.author} due to an invalid ban duration in your configuration.\n\nYou may wish to review your settings for for flair GUID: {flair_details['templateId']} in your [flair_helper wiki page](https://www.reddit.com/r/{subreddit.display_name}/wiki/edit/flair_helper)\n\nYou may also want to manually ban the user: [Ban ](https://www.reddit.com/r/{subreddit.display_name}/about/banned/?user={post.author}) since this action may not have been completed automatically due to the configuration error."
                        )
                    except asyncpraw.exceptions.RedditAPIException as e:
                        await error_handler(f"Error sending message to {subreddit.display_name}: {e}", notify_discord=True)
                    mark_action_as_completed(submission_id, 'ban')

            if not is_action_completed(submission_id, 'unban') and 'unban' in flair_details and flair_details['unban']:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - unban triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
                await subreddit.banned.remove(post.author)
                mark_action_as_completed(submission_id, 'unban')

            if not is_action_completed(submission_id, 'userFlair') and 'userFlair' in flair_details and flair_details['userFlair']['enabled']:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - set_author_flair triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None

                flair_text = flair_details['userFlair'].get('text', '')
                flair_css_class = flair_details['userFlair'].get('cssClass', '')
                flair_template_id = flair_details['userFlair'].get('templateId', '') or flair_details['userFlair'].get('templateID', '')

                # Replace placeholders in flair text and CSS class
                for placeholder, value in placeholders.items():
                    flair_text = flair_text.replace(f"{{{{{placeholder}}}}}", str(value))
                    flair_css_class = flair_css_class.replace(f"{{{{{placeholder}}}}}", str(value))

                try:
                    if flair_template_id:
                        # If template ID is provided, use it (this takes precedence)
                        await subreddit.flair.set(post.author, flair_template_id=flair_template_id)
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - Set user flair template ID to: '{flair_template_id}'") if debugmode else None
                    elif flair_text or flair_css_class:
                        # If no template ID but text or CSS class is provided, use them
                        await subreddit.flair.set(post.author, text=flair_text, css_class=flair_css_class)
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - Set user flair text to: '{flair_text}', CSS class to: '{flair_css_class}'") if debugmode else None
                    else:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - No flair settings provided, skipping flair assignment") if debugmode else None
        
                    mark_action_as_completed(submission_id, 'userFlair')
                except Exception as e:
                    await error_handler(f"Error setting user flair for {post.author} in {subreddit.display_name}: {str(e)}", notify_discord=True)



            if not is_action_completed(submission_id, 'usernote') and 'usernote' in flair_details and flair_details['usernote']['enabled']:
                usernote_note = flair_details['usernote'].get('note', '')

                if usernote_note.strip():
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - usernote triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
                    author = post_author_name
                    note_text = flair_details['usernote']['note']
                    for placeholder, value in placeholders.items():
                        note_text = note_text.replace(f"{{{{{placeholder}}}}}", str(value))
                    link = post.permalink
                    usernote_type_name = config[0]['GeneralConfiguration'].get('usernote_type_name', None)
                    await update_usernotes(subreddit, author, note_text, link, mod_name, usernote_type_name)
                    mark_action_as_completed(submission_id, 'usernote')
                else:
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Skipping usernote action due to empty usernote note on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
                    mark_action_as_completed(submission_id, 'usernote')

            if not is_action_completed(submission_id, 'contributor') and 'contributor' in flair_details and flair_details['contributor']['enabled'] and flair_details['contributor']['action'] == 'add':
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - add_contributor triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
                await subreddit.contributor.add(post.author)
                mark_action_as_completed(submission_id, 'contributor')

            if not is_action_completed(submission_id, 'contributor') and 'contributor' in flair_details and flair_details['contributor']['enabled'] and flair_details['contributor']['action'] == 'remove':
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - remove_contributor triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
                await subreddit.contributor.remove(post.author)
                mark_action_as_completed(submission_id, 'contributor')

            if not is_action_completed(submission_id, 'nuke') and 'nuke' in flair_details and flair_details['nuke'].get('enabled', False):
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - [NUKE] Nuke action invoked under Post ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None

                nuke_config = flair_details['nuke']
                ban = nuke_config.get('banFromAllListed', True)
                remove_comments = nuke_config.get('removeAllComments', True)
                remove_submissions = nuke_config.get('removeAllSubmissions', True)
                subreddits = nuke_config.get('targetSubreddits', [])

                user = post.author

                try:
                    for subreddit_name in subreddits:
                        try:
                            subreddit = await reddit.subreddit(subreddit_name)

                            if ban:
                                await subreddit.banned.add(user, ban_reason="Nuke action performed")
                                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - [NUKE] Banned user {user} from {subreddit_name}") if debugmode else None

                            if remove_comments:
                                async for comment in user.comments.new(limit=None):
                                    if comment.subreddit == subreddit_name and not comment.removed:
                                        await comment.mod.remove()
                                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - [NUKE] Removed comment {comment.id} from {subreddit_name}") if debugmode else None

                            if remove_submissions:
                                async for submission in user.submissions.new(limit=None):
                                    if submission.subreddit == subreddit_name and not submission.removed:
                                        await submission.mod.remove()
                                        await submission.mod.lock()
                                        await submission.mod.spoiler()
                                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - [NUKE] Removed submission {submission.id} from {subreddit_name}") if debugmode else None

                        except Exception as e:
                            await error_handler(f"Error performing nuke action in {subreddit_name}: {str(e)}", notify_discord=True)

                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - [NUKE] Nuke action completed for user {user} across specified subreddits {subreddits}.") if debugmode else None

                except Exception as e:
                    await error_handler(f"Error in nuke process for user {user}: {str(e)}", notify_discord=True)
                finally:
                    mark_action_as_completed(submission_id, 'nuke')

        else:
            #User Suspended or Deleted, Mark actions as complete
            mark_action_as_completed(submission_id, 'comment')
            mark_action_as_completed(submission_id, 'ban')
            mark_action_as_completed(submission_id, 'unban')
            mark_action_as_completed(submission_id, 'userFlair')
            mark_action_as_completed(submission_id, 'usernote')
            mark_action_as_completed(submission_id, 'contributor')
            mark_action_as_completed(submission_id, 'nuke')



        if not is_action_completed(submission_id, 'nukeUserComments') and flair_details.get('nukeUserComments', False):
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - nuking comments under Post ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None

            # Fetch the comments of the submission
            submission_comments = post.comments

            # Nuke the comments
            async for comment in submission_comments:
                if not comment.removed and comment.distinguished != 'moderator':  # Check if the comment is not removed and not a moderator comment
                    await comment.mod.remove()
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - removed comment {comment.id} under Post ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - finished nuking comments under Post ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
            mark_action_as_completed(submission_id, 'nukeUserComments')







# Handle Private Messages to allow the bot to reply back with a list of flairs for convenience
@reddit_error_handler
async def handle_private_messages(reddit):
    async for message in reddit.inbox.unread(limit=None):
        if isinstance(message, asyncpraw.models.Message):

            if message.body.startswith('gadzooks!'):
                if auto_accept_mod_invites:
                    subreddit = await reddit.subreddit(message.subreddit.display_name)
                    try:
                        await subreddit.mod.accept_invite()
                        print(f"Accepted mod invite for /r/{subreddit.display_name}")
                    except asyncprawcore.NotFound:
                        print(f"Invalid mod invite for /r/{subreddit.display_name}")
                    except Exception as e:
                        print(f"Failed to accept mod invite for /r/{subreddit.display_name}: {str(e)}")
                    await message.mark_read()


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


@reddit_error_handler
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
                "cssClass": "",
                "templateId": ""
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


last_startup_time_MonitorModLog = None

last_flair_data_dict = {}

# Primary Mod Log Monitor
#@reddit_error_handler
async def monitor_mod_log(reddit, bot_username, max_concurrency=1):

    global last_startup_time_MonitorModLog

    current_time = time.time()
    if last_startup_time_MonitorModLog is not None:
        elapsed_time = current_time - last_startup_time_MonitorModLog
        if elapsed_time < 10:  # Check if the bot restarted within the last 10 seconds
            delay = 10 - elapsed_time
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Bot restarted within 10 seconds. Waiting for {delay} seconds before proceeding.")
            await asyncio.sleep(delay)

    last_startup_time_MonitorModLog = current_time

    accounts_to_ignore = ['AssistantBOT1', 'anyadditionalacctshere', 'thatmayinteractwithflair']

    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Flair Helper 2 has started up successfully!\nBot username: {bot_username}") if verbosemode else None

    moderated_subreddits = []
    async for subreddit in reddit.user.moderator_subreddits():
        if f"u_{bot_username}" not in subreddit.display_name:
            #continue  # Skip the bot's own user page
            moderated_subreddits.append(subreddit.display_name)

    # Sort subreddits alphabetically ignoring case for sorting
    moderated_subreddits = sorted(moderated_subreddits, key=lambda x: x.lower())

    formatted_subreddits = "\n   ".join(moderated_subreddits)
    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: {bot_username} moderates subreddits:\n   {formatted_subreddits}") if verbosemode else None

    await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Flair Helper 2 has started up successfully!\nBot username: **{bot_username}**\n\n{bot_username} moderates subreddits:\n   {formatted_subreddits}")

    while True:
        subreddit = await reddit.subreddit("mod")
        try:
            while True:
                async for log_entry in subreddit.mod.stream.log(skip_existing=True):
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: New log entry: {log_entry.action}") if verbosemode else None

                    if log_entry.target_fullname is not None:
                        log_entry_id = log_entry.target_fullname[3:]

                        if colored_console_output:
                            disp_subreddit_displayname = colored("/r/"+log_entry.subreddit, "cyan", attrs=["underline"])
                            disp_submission_id = colored(log_entry_id, "yellow")
                        else:
                            disp_subreddit_displayname = "/r/"+log_entry.subreddit
                            disp_submission_id = log_entry_id
                    else:
                        disp_subreddit_displayname = log_entry.subreddit
                        disp_submission_id = "N/A"

                    if log_entry.action == 'wikirevise':
                        if 'flair_helper' in log_entry.details:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Flair Helper wiki page revised by {log_entry.mod} in {disp_subreddit_displayname}") if debugmode else None
                            try:
                                await fetch_and_cache_configs(reddit, bot_username, max_retries=3, retry_delay=5, single_sub=log_entry.subreddit)  # Make sure fetch_and_cache_configs is async
                            except asyncprawcore.exceptions.NotFound:
                                print(f"monitor_mod_log: Flair Helper wiki page not found in {disp_subreddit_displayname}") if debugmode else None
                                errors_logger.error(f"monitor_mod_log: Flair Helper wiki page not found in /r/{log_entry.subreddit}")

                    elif (log_entry.action == 'editflair'
                          and log_entry.mod not in accounts_to_ignore
                          and log_entry.target_fullname is not None
                          and log_entry.target_fullname.startswith('t3_')):
                        # This is a link (submission) flair edit
                        submission_id = log_entry.target_fullname[3:]  # Remove the 't3_' prefix
                        config = get_cached_config(log_entry.subreddit)

                        if config is not None:
                            post = await reddit.submission(submission_id)
                            flair_guid = getattr(post, 'link_flair_template_id', None)  # Use getattr to safely retrieve the attribute

                            if flair_guid is not None:
                                last_flair_data_key = f"{submission_id}_{flair_guid}"
                                #print(f"last_flair_data_key: {last_flair_data_key}") if debugmode else None
                                current_time = time.time()

                                if last_flair_data_key not in last_flair_data_dict or current_time - last_flair_data_dict[last_flair_data_key] >= config[0]['GeneralConfiguration'].get('ignore_same_flair_seconds', 60):
                                    last_flair_data_dict[last_flair_data_key] = current_time

                                    if colored_console_output:
                                        disp_flair_guid = colored(flair_guid, "magenta")
                                    else:
                                        disp_flair_guid = flair_guid

                                    flair_details = next((flair for flair in config[1:] if flair['templateId'] == flair_guid), None)

                                    if flair_details is not None:
                                        actions = []

                                        if flair_details.get('approve', False):
                                            actions.append('approve')
                                        if flair_details.get('remove', False):
                                            actions.append('remove')
                                        if flair_details.get('lock', False):
                                            actions.append('lock')
                                        if flair_details.get('spoiler', False):
                                            actions.append('spoiler')
                                        if flair_details.get('clearPostFlair', False):
                                            actions.append('clearPostFlair')
                                        if flair_details.get('modlogReason', '').strip():
                                            actions.append('modlogReason')
                                        if flair_details.get('comment', {}).get('enabled', False):
                                            actions.append('comment')
                                        if flair_details.get('nukeUserComments', False):
                                            actions.append('nukeUserComments')
                                        if flair_details.get('usernote', {}).get('enabled', False):
                                            actions.append('usernote')
                                        if flair_details.get('contributor', {}).get('enabled', False):
                                            actions.append('contributor')
                                        if flair_details.get('userFlair', {}).get('enabled', False):
                                            actions.append('userFlair')
                                        if flair_details.get('ban', {}).get('enabled', False):
                                            actions.append('ban')
                                        if flair_details.get('unban', False):
                                            actions.append('unban')
                                        if flair_details.get('sendToWebhook', False):
                                            actions.append('sendToWebhook')

                                        if actions:
                                            insert_actions_to_database(submission_id, actions, log_entry.mod.name, flair_guid)
                                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Actions for flair GUID {disp_flair_guid} under submission {disp_submission_id} in {disp_subreddit_displayname} added to the database") if debugmode else None
                                        else:
                                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: No actions found for flair GUID {disp_flair_guid}") if debugmode else None
                                    else:
                                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Flair GUID {disp_flair_guid} not found in the configuration") if debugmode else None
                                else:
                                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Ignoring duplicate flair assignment for submission {submission_id} with flair GUID {flair_guid}") if debugmode else None
                            else:
                                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Flair GUID not found for submission {submission_id}") if debugmode else None
                        else:
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Configuration not found for /r/{disp_subreddit_displayname}") if debugmode else None

        except asyncprawcore.exceptions.RequestException as e:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Error in mod log stream: {str(e)}. Retrying...") if debugmode else None
            await asyncio.sleep(5)  # Wait for a short interval before retrying



async def process_flair_actions(reddit, max_concurrency=2):
    semaphore = asyncio.Semaphore(max_concurrency)

    async def process_flair_assignment_with_semaphore(submission_id, mod_name):

        if colored_console_output:
            disp_submission_id = colored(submission_id, "yellow")
        else:
            disp_submission_id = submission_id

        async with semaphore:
            try:
                post = await reddit.submission(submission_id)
                subreddit = post.subreddit
                config = get_cached_config(subreddit.display_name)

                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Sending {submission_id} for processing") if debugmode else None
                await process_flair_assignment(reddit, post, config, subreddit, mod_name)

                if is_submission_completed(submission_id):
                    delete_completed_actions(submission_id)
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: All actions for submission {disp_submission_id} completed and deleted from the database") if debugmode else None

            except Exception as e:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Error processing actions for submission {disp_submission_id}: {str(e)}") if debugmode else None
                # Handle the error appropriately (e.g., log, retry, or skip)

    while True:
        pending_submission_ids = get_pending_submission_ids_from_database()

        unique_submission_ids = set()
        tasks = []
        for submission_id, mod_name in pending_submission_ids:
            if submission_id not in unique_submission_ids:
                unique_submission_ids.add(submission_id)

                # Check if all actions for the submission are completed
                if is_submission_completed(submission_id):
                    delete_completed_actions(submission_id)
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: All actions for submission {submission_id} completed. Skipping processing.") if debugmode else None
                    continue

                if colored_console_output:
                    disp_modname = colored(mod_name, "green", attrs=["underline", "bold"])
                else:
                    disp_modname = mod_name

                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Pending Action {submission_id} actioned by {disp_modname} found in database") if debugmode else None
                task = asyncio.create_task(process_flair_assignment_with_semaphore(submission_id, mod_name))
                tasks.append(task)

        await asyncio.gather(*tasks)

        await asyncio.sleep(1)  # Adjust the delay as needed



@reddit_error_handler
async def delayed_fetch_and_cache_configs(reddit, bot_username, delay):
    await asyncio.sleep(delay)  # Wait for the specified delay
    #print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: fetch_and_cache_configs skipped during testing.") if debugmode else None
    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Waited {delay}, Now processing fetch_and_cache_configs.") if debugmode else None
    await fetch_and_cache_configs(reddit, bot_username)  # Fetch and cache configurations


# Check for PM's every 60 seconds
@reddit_error_handler
async def monitor_private_messages(reddit):
    while True:
        await handle_private_messages(reddit)
        await asyncio.sleep(120)  # Sleep for 60 seconds before the next iteration








async def start_monitor_mod_log_task(reddit, bot_username):
    while True:
        try:
            await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [Start Task] Mod Log Monitoring Started.")
            await monitor_mod_log(reddit, bot_username)
        except Exception as e:
            #print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [Start Modqueue Task] Error in modqueue task: {str(e)}\nWaiting 60 seconds before restarting monitor_mod_queue") if debugmode else None
            await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [Start Task] Error in monitor_mod_log task: {str(e)}\nWaiting 20 seconds before restarting monitor_mod_log")
            await asyncio.sleep(20)
            await add_task('start_monitor_mod_log_task', start_task, start_monitor_mod_log_task, reddit, bot_username)
            return

async def start_process_flair_actions_task(reddit, max_concurrency=2):
    while True:
        try:
            await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [Start Task] Flair Action Monitoring Started.")
            await process_flair_actions(reddit, max_concurrency)
        except Exception as e:
            #print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [Start Modmail Task] Error in modmail task: {str(e)}\nWaiting 60 seconds before restarting monitor_modmail_stream") if debugmode else None
            await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [Start Task] Error in process_flair_actions task: {str(e)}\nWaiting 20 seconds before restarting monitor_modmail_stream")
            await asyncio.sleep(20)
            await add_task('start_process_flair_actions_task', start_task, start_process_flair_actions_task, reddit, max_concurrency)
            return

async def start_monitor_private_messages_task(reddit):
    while True:
        try:
            await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [Start Task] Private Message Monitoring Started.")
            await monitor_private_messages(reddit)
        except Exception as e:
            #print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [Start Submission Task] Error in submission task: {str(e)}\nWaiting 60 seconds before restarting start_submission_task") if debugmode else None
            await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [Start Task] Error in monitor_private_messages task: {str(e)}\nWaiting 20 seconds before restarting monitor_submission_stream")
            await asyncio.sleep(20)
            await add_task('start_monitor_private_messages_task', start_task, start_monitor_private_messages_task, reddit)
            return







last_startup_time_main = None

running_tasks = {}

task_semaphore = asyncio.Semaphore(3)  # Limit to 3 concurrent task restarts

async def add_task(task_name, task_func, *args):
    global running_tasks
    async with task_semaphore:
        if task_name in running_tasks:
            running_tasks[task_name].cancel()
            await error_handler(f"[Task Management] Cancelling Task {task_name}", notify_discord=True)
            await asyncio.sleep(2)  # Wait for a short duration to allow the task to be cancelled properly
            await error_handler(f"[Task Management] Restarting Task {task_name}", notify_discord=True)
        task = asyncio.create_task(task_func(*args))
        running_tasks[task_name] = task
        return task



async def start_task(task_func, *args):
    initial_delay = 10  # Initial delay in seconds
    max_delay = 160  # Max delay in seconds
    delay = initial_delay

    while True:
        try:
            await task_func(*args)
        except Exception as e:
            #print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [Start Task] Error in {task_func.__name__} task: {str(e)}\nRetrying in {delay} seconds...") if debugmode else None
            await error_handler(f"[Start Task] Error in {task_func.__name__} task: {str(e)}\nRetrying in {delay} seconds...", notify_discord=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)  # Double the delay, capped at max_delay
        else:
            delay = initial_delay  # Reset the delay on successful execution



@reddit_error_handler
async def bot_main():
    global running_tasks

    if verbosemode:
        action_type = "[Initialization] "
    else:
        action_type = "[Initialization] "

    create_actions_database()

    global last_startup_time_main

    current_time = time.time()
    if last_startup_time_main is not None:
        elapsed_time = current_time - last_startup_time_main
        if elapsed_time < 10:  # Check if the bot restarted within the last 10 seconds
            delay = 10 - elapsed_time
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Bot restarted within 10 seconds. Waiting for {delay} seconds before proceeding.") if debugmode else None
            await asyncio.sleep(delay)

    last_startup_time_main = current_time


    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Flair Helper 2 initializing asyncpraw") if debugmode else None
    reddit = asyncpraw.Reddit("fh2_login")

    # Fetch the bot's username
    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Flair Helper 2 fetching bot_username") if debugmode else None
    me = await reddit.user.me()
    bot_username = me.name
    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Flair Helper 2 fetched bot username: {bot_username}") if debugmode else None


    # Check if the database is empty
    if is_config_database_empty():
        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Database is empty. Fetching and caching configurations for all moderated subreddits.") if verbosemode else None
        await fetch_and_cache_configs(reddit, bot_username)

    max_concurrency = 2
    wiki_fetch_delay = 90

    await add_task('start_monitor_mod_log_task', start_task, start_monitor_mod_log_task, reddit, bot_username)
    await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: {action_type}Starting Monitor Mod Log...")

    await add_task('start_process_flair_actions_task', start_task, start_process_flair_actions_task, reddit, max_concurrency)
    await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: {action_type}Starting Process Flair Actions...")

    await add_task('start_monitor_private_messages_task', start_task, start_monitor_private_messages_task, reddit)
    await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: {action_type}Starting Monitor Private Messages...")

    await delayed_fetch_and_cache_configs(reddit, bot_username, wiki_fetch_delay)

    try:
        await asyncio.gather(*running_tasks.values())
    except asyncio.CancelledError:
        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: {action_type}Tasks cancelled") if debugmode else None


def main():
    loop = asyncio.get_event_loop()
    asyncio.ensure_future(bot_main())
    loop.run_forever()

if __name__ == "__main__":
    #asyncio.run(main())
    #asyncio.get_event_loop().run_until_complete(bot_main())
    main()
