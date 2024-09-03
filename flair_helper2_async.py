import aiohttp
import asyncio
import asyncpraw
import asyncprawcore
import sqlite3
import yaml
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Callable, Any, Dict
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


if config.telegram_bot_control:
    # For optionally being able to restart the bot via a Telegram bot username
    from telebot import types
    from telebot.apihelper import ApiTelegramException
    from telebot.asyncio_helper import ApiTelegramException
    from telebot.async_telebot import AsyncTeleBot
    from telebot.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

    telegram_bot = AsyncTeleBot(config.telegram_TOKEN)
    admin_ids = config.telegram_admin_ids


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


def check_restriction_status(message):
    restriction_status = read_rate_limit_config()

    # Immediate return for admin messages to ensure URL processing
    if message.from_user.id in config.telegram_admin_ids:
        return "admin_private"
    else:
        return "unauthorized"


@telegram_bot.message_handler(commands=['status'])
async def handle_status_command(message):
    if message.chat.type in ("private") and message.chat.id in config.telegram_admin_ids:
        status_message = "Flair Helper 2 Status Report:\n\n"

        # Current time
        status_message += f"Current time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"

        # Memory usage
        import psutil
        process = psutil.Process()
        memory_usage = process.memory_info().rss / 1024 / 1024  # in MB
        status_message += f"Memory usage: {memory_usage:.2f} MB\n\n"

        # Running tasks
        status_message += "Running Tasks:\n"
        for task_name in running_tasks.keys():
            status_message += f"- {task_name}\n"
        status_message += "\n"

        # Monitored subreddits
        conn = sqlite3.connect('flair_helper_configs.db')
        c = conn.cursor()
        c.execute("SELECT subreddit FROM configs")
        subreddits = c.fetchall()
        subreddit_count = len(subreddits)

        status_message += f"Monitored Subreddits: {subreddit_count}\n"
        if subreddit_count > 0:
            status_message += "Subreddits:\n"
            for subreddit in sorted(subreddits, key=lambda x: x[0].lower()):
                status_message += f"- {subreddit[0]}\n"
        status_message += "\n"

        # Pending actions in database
        conn = sqlite3.connect('flair_helper_actions.db')
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM actions WHERE completed = 0")
        pending_count = c.fetchone()[0]

        if pending_count > 0:
            status_message += f"Pending Actions: {pending_count}\n"
            c.execute("SELECT submission_id, action, mod_name FROM actions WHERE completed = 0 LIMIT 20")
            pending_actions = c.fetchall()
            status_message += "Recent pending actions (up to 20):\n"
            for action in pending_actions:
                status_message += f"- Submission {action[0]}: {action[1]} by {action[2]}\n"
        else:
            status_message += "No pending actions in the actions database.\n"

        conn.close()

        await telegram_bot.send_message(chat_id=message.chat.id, text=status_message)


@telegram_bot.message_handler(commands=['restart'])
async def handle_restart_command(message):
    if message.chat.type in ("private") and message.chat.id in config.telegram_admin_ids:
        #await telegram_bot.send_message(chat_id=message.chat.id, text="Restarting bot...")
        #os._exit(0)  # Forcefully exit the process
        user_name = message.from_user.username or message.from_user.first_name
        await error_handler(f"Telegram 'restart' command received from {user_name}", notify_discord=True)

        try:
            await telegram_bot.send_message(chat_id=message.chat.id, text=f"Restarting start_process_flair_actions_task...")
            await add_task('Flair Helper - Process Flair Actions', start_task, start_process_flair_actions_task, reddit, max_concurrency, max_processing_retries, processing_retry_delay)
            #await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: {action_type}Starting Process Flair Actions...")
        except Exception as e:
            await telegram_bot.send_message(chat_id=message.chat.id, text=f"**There was an error restarting the start_process_flair_actions_task...**\n Exception: {e}")

        try:
            await telegram_bot.send_message(chat_id=message.chat.id, text=f"Restarting start_monitor_mod_log_task...")
            await add_task('Reddit - Monitor Mod Log', start_task, start_monitor_mod_log_task, reddit, bot_username)
            #await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: {action_type}Starting Monitor Mod Log...")
        except Exception as e:
            await telegram_bot.send_message(chat_id=message.chat.id, text=f"**There was an error restarting the start_monitor_mod_log_task...**\n Exception: {e}")

        try:
            await telegram_bot.send_message(chat_id=message.chat.id, text=f"Restarting start_monitor_private_messages_task...")
            await add_task('Reddit - Monitor Private Messages', start_task, start_monitor_private_messages_task, reddit)
            #await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: {action_type}Starting Monitor Private Messages...")
        except Exception as e:
            await telegram_bot.send_message(chat_id=message.chat.id, text=f"**There was an error restarting the start_monitor_private_messages_task...**\n Exception: {e}")



@telegram_bot.message_handler(commands=['kill'])
async def handle_restart_command(message):
    if message.chat.type in ("private") and message.chat.id in config.telegram_admin_ids:

        user_name = message.from_user.username or message.from_user.first_name
        await error_handler(f"Telegram 'kill' command received from {user_name}", notify_discord=True)

        await telegram_bot.send_message(chat_id=message.chat.id, text="Forcefully Killing Bot...")
        os._exit(0)  # Forcefully exit the process


@telegram_bot.message_handler(func=lambda message: True, content_types=['new_chat_members'])
async def handle_new_chat_members(message: Message):
    # Check if our bot is among the new members
    for member in message.new_chat_members:
        if member.id == telegram_bot_id:  # Use telegram_bot_id instead of making another async call
            timestr = time.strftime("%Y-%m-%d %H:%M:%S ")
            chat_id = message.chat.id
            user_name = message.from_user.username or message.from_user.first_name
            group_name = message.chat.title
            actions_str = f"**BOT LEFT {message.chat.type}** | A User {user_name} Attempted action under {message.chat.type}: {group_name} ID: {chat_id}"
            print(f"{timestr} {actions_str}") if debugmode or verbosemode else None
            actions_logger.info(actions_str)
            await telegram_bot.send_message(128629760, f"**BOT LEFT {message.chat.type}** | A User {user_name} attempted action under {message.chat.type}: {group_name} ID: {chat_id}")

            await telegram_bot.leave_chat(message.chat.id)



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


async def send_failure_notification(submission_id, mod_name, error_message):
    # Fetch pending actions
    pending_actions = get_pending_actions(submission_id)

    # Create a formatted list of pending actions
    action_list = "\n".join([f"- {action}" for action in pending_actions])

    # Create the short link
    short_link = f"https://redd.it/{submission_id}"

    notification = (
        f"Failed to process submission {submission_id} after {max_retries} attempts.\n"
        f"Last error: {error_message}\n"
        f"Mod: {mod_name}\n"
        f"Short link: {short_link}\n\n"
        f"Pending actions:\n{action_list}"
    )

    # Send Discord notification
    if discord_bot_notifications:
        await discord_status_notification(notification)

    # Send Telegram notification
    if config.telegram_bot_control:
        for admin_id in config.telegram_admin_ids:
            await telegram_bot.send_message(admin_id, notification)

    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Sent failure notification for submission {submission_id}") if debugmode else None



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

def get_pending_actions(submission_id):
    conn = sqlite3.connect('flair_helper_actions.db')
    c = conn.cursor()
    c.execute("SELECT action FROM actions WHERE submission_id = ? AND completed = 0", (submission_id,))
    pending_actions = [row[0] for row in c.fetchall()]
    conn.close()
    return pending_actions

def mark_action_as_completed(submission_id, action):
    conn = sqlite3.connect('flair_helper_actions.db')
    c = conn.cursor()
    c.execute("UPDATE actions SET completed = 1 WHERE submission_id = ? AND action = ?", (submission_id, action))
    conn.commit()
    conn.close()

def mark_all_actions_completed(submission_id):
    conn = sqlite3.connect('flair_helper_actions.db')
    c = conn.cursor()
    c.execute("UPDATE actions SET completed = 1 WHERE submission_id = ?", (submission_id,))
    conn.commit()
    conn.close()
    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Marked all actions as completed for submission {submission_id} due to repeated failures") if debugmode else None

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


async def add_escalating_ban_note(subreddit, author, ban_duration, link, mod_name):
    if ban_duration == 0:
        note_text = "FH-Ban-permanent"
    else:
        note_text = f"FH-Ban-{ban_duration}"
    usernote_type_name = "flair_helper_note"
    await update_usernotes(subreddit, author, note_text, link, mod_name, usernote_type_name)


@reddit_error_handler
async def get_usernotes(subreddit, username, max_retries=3, retry_delay=5):
    async with usernotes_lock:
        for attempt in range(max_retries):
            try:
                usernotes_wiki = await subreddit.wiki.get_page("usernotes")
                usernotes_content = usernotes_wiki.content_md
                usernotes_data = json.loads(usernotes_content)

                if 'blob' not in usernotes_data:
                    return []  # No usernotes exist

                decompressed_notes = decompress_notes(usernotes_data['blob'])

                if username not in decompressed_notes:
                    return []  # No notes for this user

                user_notes = decompressed_notes[username]['ns']

                # Convert the notes to the format we need for ban tracking
                formatted_notes = []
                for note in user_notes:
                    note_text = note['n']
                    if note_text.startswith("[FH] FH-Ban-"):
                        ban_value = note_text.split("FH-Ban-")[1]
                        formatted_notes.append(ban_value)

                return formatted_notes

            except Exception as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                else:
                    error_message = f"Failed to retrieve usernotes for user {username}"
                    print(error_message) if debugmode else None
                    await error_handler(error_message, notify_discord=True)
                    return []  # Return an empty list if we can't retrieve the notes

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


def get_display_name(author, use_color=False):
    if author is None:
        return colored("[deleted]", "red") if use_color else "[deleted]"
    name = getattr(author, 'name', '[unknown]')
    return colored(name, "light_red") if use_color else name


def parse_ban_duration_list(duration_str):
    return [int(d) if d.isdigit() else 0 for d in duration_str.split(',')]


async def get_next_ban_duration(subreddit, user, duration_list):
    print(f"Debug: Entering get_next_ban_duration for user {user}") if verbosemode else None
    print(f"Debug: Duration list: {duration_list}") if verbosemode else None

    usernotes = await get_usernotes(subreddit, user)
    print(f"Debug: Retrieved usernotes: {usernotes}") if verbosemode else None

    if not usernotes:
        next_duration = duration_list[0]
        print(f"Debug: No relevant notes, returning first duration: {next_duration}") if verbosemode else None
        return next_duration

    highest_duration = max([int(note) if note != 'permanent' else float('inf') for note in usernotes])
    print(f"Debug: Highest previous duration: {highest_duration}") if verbosemode else None

    if highest_duration == float('inf'):
        return 0  # Return 0 for permanent ban

    next_duration = duration_list[-1]  # Default to the last (highest) duration
    for duration in duration_list:
        if duration > highest_duration:
            next_duration = duration
            break

    print(f"Debug: Returning next duration: {next_duration}") if verbosemode else None
    return next_duration


def get_ban_duration_string(duration):
    if duration == 0:
        return "permanently banned", "permanent"
    elif duration == 1:
        return "banned for 1 day", "1"
    else:
        return f"banned for {duration} days", str(duration)


async def apply_escalating_ban(subreddit, user, duration_list, ban_message, mod_note, mod_name, link):
    try:
        print(f"Debug: Entering apply_escalating_ban for user {user.name}") if verbosemode else None
        print(f"Debug: Ban Duration list: {duration_list}") if debugmode or verbosemode else None

        next_duration = await get_next_ban_duration(subreddit, user.name, duration_list)
        print(f"Debug: Next duration: {next_duration}") if debugmode or verbosemode else None

        ban_duration_string, ban_duration_number = get_ban_duration_string(next_duration)
        print(f"Debug: Ban duration string: {ban_duration_string}") if verbosemode else None
        print(f"Debug: Ban duration number: {ban_duration_number}") if verbosemode else None

        # Replace the placeholders in the ban message and mod note
        ban_message = ban_message.replace("{{ban_duration}}", ban_duration_string)
        ban_message = ban_message.replace("{{ban_duration_number}}", ban_duration_number)
        mod_note = mod_note.replace("{{ban_duration}}", ban_duration_string)
        mod_note = mod_note.replace("{{ban_duration_number}}", ban_duration_number)

        print(f"Debug: Final ban message: {ban_message}") if verbosemode else None
        print(f"Debug: Final mod note: {mod_note}") if verbosemode else None

        if next_duration == 0:
            await subreddit.banned.add(user, ban_message=ban_message, note=mod_note)
        else:
            await subreddit.banned.add(user, duration=next_duration, ban_message=ban_message, note=mod_note)

        await add_escalating_ban_note(subreddit, user.name, next_duration, link, mod_name)
        print(f"Applied escalating ban: FH-Ban-{ban_duration_number} to user {user.name}") if debugmode or verbosemode else None
    except Exception as e:
        await error_handler(f"Error applying escalating ban to user {user.name}: {str(e)}", notify_discord=True)
        print(f"Debug: Exception in apply_escalating_ban: {str(e)}") if debugmode or verbosemode else None
        print(f"Debug: Exception traceback: {traceback.format_exc()}") if debugmode or verbosemode else None











async def handle_approve_action(post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname):
    #if not is_action_completed(submission_id, 'approve') and 'approve' in flair_details and flair_details['approve']:
    try:
        if not hasattr(post, '_fetched') or not post._fetched:
            await post.load()

        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - Approve triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
        if post.removed:
            await post.mod.approve()
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - Submission approved on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
        else:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - Submission already approved on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
        if post.locked:
            await post.mod.unlock()
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - Submission unlocked on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
        if post.spoiler:
            await post.mod.unspoiler()
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - Spoiler removed on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
        mark_action_as_completed(submission_id, 'approve')
    except Exception as e:
        await error_handler(f"Error in handle_approve_action for {disp_submission_id}: {str(e)}", notify_discord=True)


async def handle_remove_action(post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname):
    #if not is_action_completed(submission_id, 'remove') and 'remove' in flair_details and flair_details['remove']:
    try:
        if not hasattr(post, '_fetched') or not post._fetched:
            await post.load()

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
    except Exception as e:
        await error_handler(f"Error in handle_remove_action for {disp_submission_id}: {str(e)}", notify_discord=True)


async def handle_modlog_reason_action(post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname):
    try:
        if not hasattr(post, '_fetched') or not post._fetched:
            await post.load()

        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - modlogReason triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None

        mod_note = flair_details.get('modlogReason', '')[:250]  # Truncate to 250 characters

        if mod_note:
            await post.mod.create_note(note=mod_note)
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - Added mod note: '{mod_note}' to ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
        else:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - No mod note provided for ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None

        mark_action_as_completed(submission_id, 'modlogReason')
    except Exception as e:
        await error_handler(f"Error in handle_modlog_reason_action for {disp_submission_id}: {str(e)}", notify_discord=True)


async def handle_lock_action(post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname):
    #if not is_action_completed(submission_id, 'lock') and 'lock' in flair_details and flair_details['lock']:
    try:
        if not hasattr(post, '_fetched') or not post._fetched:
            await post.load()

        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - lock triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
        if not post.locked:
            await post.mod.lock()
            mark_action_as_completed(submission_id, 'lock')
        else:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Post {disp_submission_id} is already locked. Marking action as completed.") if debugmode else None
            mark_action_as_completed(submission_id, 'lock')
    except Exception as e:
        await error_handler(f"Error in handle_lock_action for {disp_submission_id}: {str(e)}", notify_discord=True)


async def handle_spoiler_action(post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname):
    #if not is_action_completed(submission_id, 'spoiler') and 'spoiler' in flair_details and flair_details['spoiler']:
    try:
        if not hasattr(post, '_fetched') or not post._fetched:
            await post.load()

        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - spoiler triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
        if not post.spoiler:
            await post.mod.spoiler()
            mark_action_as_completed(submission_id, 'spoiler')
        else:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Post {disp_submission_id} is already spoilered. Marking action as completed.") if debugmode else None
            mark_action_as_completed(submission_id, 'spoiler')
    except Exception as e:
        await error_handler(f"Error in handle_spoiler_action for {disp_submission_id}: {str(e)}", notify_discord=True)


async def handle_clear_post_flair_action(post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname):
    #if not is_action_completed(submission_id, 'clearPostFlair') and 'clearPostFlair' in flair_details and flair_details['clearPostFlair']:
    try:
        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - remove_link_flair triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
        await post.mod.flair(text='', css_class='')
        mark_action_as_completed(submission_id, 'clearPostFlair')
    except Exception as e:
        await error_handler(f"Error in handle_clear_post_flair_action for {disp_submission_id}: {str(e)}", notify_discord=True)


async def handle_webhook_action(config, post, flair_text, mod_name, flair_guid, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname):
    #if not is_action_completed(submission_id, 'sendToWebhook') and 'sendToWebhook' in flair_details and flair_details['sendToWebhook']:
    try:
        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - send_to_webhook triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
        send_webhook_notification(config, post, flair_text, mod_name, flair_guid)
        mark_action_as_completed(submission_id, 'sendToWebhook')
    except Exception as e:
        await error_handler(f"Error in handle_webhook_action for {disp_submission_id}: {str(e)}", notify_discord=True)


async def handle_comment_action(post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname, config, formatted_removal_reason_comment):
    #if not is_action_completed(submission_id, 'comment') and 'comment' in flair_details and flair_details['comment']['enabled']:
    try:
        post_age_days = (datetime.utcnow() - datetime.utcfromtimestamp(post.created_utc)).days
        max_age = config[0]['GeneralConfiguration'].get('maxAgeForComment', 175)
        if post_age_days <= max_age:
            comment_body = flair_details['comment'].get('body', '')
            if comment_body.strip():
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - comment triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
                if flair_details['remove']:
                    removal_type = config[0]['GeneralConfiguration'].get('removal_comment_type', '')
                    if removal_type == '':
                        removal_type = 'public_as_subreddit'
                    elif removal_type not in ['public', 'private', 'private_exposed', 'public_as_subreddit']:
                        removal_type = 'public_as_subreddit'
                    await post.mod.send_removal_message(message=formatted_removal_reason_comment, type=removal_type)
                else:
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
            mark_action_as_completed(submission_id, 'comment')
    except Exception as e:
        await error_handler(f"Error in handle_comment_action for {disp_submission_id}: {str(e)}", notify_discord=True)


async def handle_ban_action(subreddit, post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname, placeholders, mod_name):
    #if not is_action_completed(submission_id, 'ban') and 'ban' in flair_details and flair_details['ban']['enabled']:
    try:
        ban_duration = flair_details['ban'].get('duration', '')
        ban_message = flair_details['ban']['message']
        ban_reason = flair_details['ban']['modNote']

        for placeholder, value in placeholders.items():
            ban_message = ban_message.replace(f"{{{{{placeholder}}}}}", str(value))
            ban_reason = ban_reason.replace(f"{{{{{placeholder}}}}}", str(value))[:100]

        if isinstance(ban_duration, str) and ',' in ban_duration:
            duration_list = parse_ban_duration_list(ban_duration)
            await apply_escalating_ban(subreddit, post.author, duration_list, ban_message, ban_reason, mod_name, post.permalink)
        else:
            if ban_duration == '' or ban_duration is True:
                await subreddit.banned.add(post.author, ban_message=ban_message, ban_reason=ban_reason)
            elif isinstance(ban_duration, int) and ban_duration > 0:
                await subreddit.banned.add(post.author, ban_message=ban_message, ban_reason=ban_reason, duration=ban_duration)
            else:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Skipping ban action due to invalid ban duration on ID: {disp_submission_id} for flair GUID: {flair_details['templateId']} in {disp_subreddit_displayname}") if debugmode else None
                return
        mark_action_as_completed(submission_id, 'ban')
    except Exception as e:
        await error_handler(f"Error in handle_ban_action for {disp_submission_id}: {str(e)}", notify_discord=True)


async def handle_unban_action(subreddit, post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname):
    #if not is_action_completed(submission_id, 'unban') and 'unban' in flair_details and flair_details['unban']:
    try:
        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - unban triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
        await subreddit.banned.remove(post.author)
        mark_action_as_completed(submission_id, 'unban')
    except Exception as e:
        await error_handler(f"Error in handle_unban_action for {disp_submission_id}: {str(e)}", notify_discord=True)


async def handle_user_flair_action(subreddit, post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname, placeholders):
    #if not is_action_completed(submission_id, 'userFlair') and 'userFlair' in flair_details and flair_details['userFlair']['enabled']:
    try:
        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - set_author_flair triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None

        flair_text = flair_details['userFlair'].get('text', '')
        flair_css_class = flair_details['userFlair'].get('cssClass', '')
        flair_template_id = flair_details['userFlair'].get('templateId', '') or flair_details['userFlair'].get('templateID', '')

        for placeholder, value in placeholders.items():
            flair_text = flair_text.replace(f"{{{{{placeholder}}}}}", str(value))
            flair_css_class = flair_css_class.replace(f"{{{{{placeholder}}}}}", str(value))

        try:
            if flair_template_id:
                await subreddit.flair.set(post.author, flair_template_id=flair_template_id)
            elif flair_text or flair_css_class:
                await subreddit.flair.set(post.author, text=flair_text, css_class=flair_css_class)
            mark_action_as_completed(submission_id, 'userFlair')
        except Exception as e:
            await error_handler(f"Error setting user flair for {post.author} in {subreddit.display_name}: {str(e)}", notify_discord=True)
    except Exception as e:
        await error_handler(f"Error in handle_user_flair_action for {disp_submission_id}: {str(e)}", notify_discord=True)


async def handle_usernote_action(subreddit, post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname, placeholders, config, mod_name):
    #if not is_action_completed(submission_id, 'usernote') and 'usernote' in flair_details and flair_details['usernote']['enabled']:
    try:
        usernote_note = flair_details['usernote'].get('note', '')
        if usernote_note.strip():
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - usernote triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
            author = post.author.name
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
    except Exception as e:
        await error_handler(f"Error in handle_usernote_action for {disp_submission_id}: {str(e)}", notify_discord=True)


async def handle_contributor_action(subreddit, post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname):
    #if not is_action_completed(submission_id, 'contributor') and 'contributor' in flair_details and flair_details['contributor']['enabled']:
    try:
        if flair_details['contributor']['action'] == 'add':
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - add_contributor triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
            await subreddit.contributor.add(post.author)
        elif flair_details['contributor']['action'] == 'remove':
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - remove_contributor triggered on ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
            await subreddit.contributor.remove(post.author)
        mark_action_as_completed(submission_id, 'contributor')
    except Exception as e:
        await error_handler(f"Error in handle_contributor_action for {disp_submission_id}: {str(e)}", notify_discord=True)


async def handle_nuke_action(reddit, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname, post):
    #if not is_action_completed(submission_id, 'nuke') and 'nuke' in flair_details and flair_details['nuke'].get('enabled', False):
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


async def handle_nuke_user_comments_action(post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname):
    #if not is_action_completed(submission_id, 'nukeUserComments') and flair_details.get('nukeUserComments', False):
    try:
        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - nuking comments under Post ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None

        submission_comments = post.comments

        async for comment in submission_comments:
            if not comment.removed and comment.distinguished != 'moderator':
                await comment.mod.remove()
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - removed comment {comment.id} under Post ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None

        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: - finished nuking comments under Post ID: {disp_submission_id} in {disp_subreddit_displayname}") if debugmode else None
        mark_action_as_completed(submission_id, 'nukeUserComments')
    except Exception as e:
        await error_handler(f"Error in handle_nuke_user_comments_action for {disp_submission_id}: {str(e)}", notify_discord=True)




# Primary process to handle any flair changes that appear in the logs
async def process_flair_assignment(reddit, post, config, subreddit, mod_name, max_retries=3, retry_delay=5):
    submission_id = post.id
    flair_guid = getattr(post, 'link_flair_template_id', None)
    flair_details = next((flair for flair in config[1:] if flair['templateId'] == flair_guid), None)

    # Initialize variables
    post_author_name = "[deleted]"
    is_author_deleted = True
    is_author_suspended = False
    author_id = None
    subreddit_id = None

    if colored_console_output:
        disp_subreddit_displayname = colored("/r/"+subreddit.display_name, "cyan", attrs=["underline"])
        disp_submission_id = colored(submission_id, "yellow")
        disp_flair_guid = colored(flair_guid, "magenta")
    else:
        disp_subreddit_displayname = subreddit.display_name
        disp_submission_id = submission_id
        disp_flair_guid = flair_guid

    if flair_details is None:
        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Flair GUID {disp_flair_guid} not found in the configuration for {disp_subreddit_displayname}") if debugmode else None
        return

    # Reload the configuration from the database
    config = get_cached_config(subreddit.display_name)

    if config is None:
        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Configuration not found for {disp_subreddit_displayname}. Skipping flair assignment.")
        return

    if flair_guid and any(flair['templateId'] == flair_guid for flair in config[1:]):
        user_info = ""
        author_details = ""

        try:
            await post.load()

            if post.author is None:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: No author found for post ID: {disp_submission_id}. Post is likely deleted.") if debugmode else None
                # Mark all actions as completed for deleted posts

                await handle_remove_action(post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname)
                await handle_lock_action(post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname)

                # Mark other actions as completed
                #actions_to_complete = ['comment', 'approve', 'remove', 'lock', 'modlogReason', 'ban', 'unban', 'userFlair', 'usernote', 'contributor', 'sendToWebhook']
                actions_to_complete = ['comment', 'approve', 'modlogReason', 'ban', 'unban', 'userFlair', 'usernote', 'contributor', 'sendToWebhook']
                #for action in actions_to_complete:
                #    mark_action_as_completed(submission_id, action)
                #    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Marked action '{action}' as completed for deleted post {disp_submission_id}") if debugmode else None

                for action in actions_to_complete:
                    if action in flair_details:
                        should_mark_completed = False
                        if action == 'comment':
                            should_mark_completed = flair_details[action].get('enabled', False)
                        elif action in ['userFlair', 'ban']:
                            should_mark_completed = flair_details[action].get('enabled', False)
                        else:
                            should_mark_completed = bool(flair_details[action])

                        if should_mark_completed:
                            mark_action_as_completed(submission_id, action)
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Marked action '{action}' as completed for deleted post {disp_submission_id}") if debugmode else None

                # Delete all actions for this submission from the database
                delete_completed_actions(submission_id)
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Deleted all actions for deleted post {disp_submission_id} from database") if debugmode else None
                return  # Exit the function early for deleted posts

            # Load subreddit data
            try:
                await subreddit.load()
                subreddit_id = subreddit.id
            except AttributeError:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Unable to fetch subreddit ID for {subreddit.display_name}") if debugmode else None

            disp_author_name = get_display_name(post.author, colored_console_output)

            if post.author:
                is_author_deleted = False
                post_author_name = post.author.name
                await post.author.load()
                is_author_suspended = hasattr(post.author, 'is_suspended') and post.author.is_suspended
                author_id = None if is_author_suspended else getattr(post.author, 'id', None)

                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Author Info: Username: {post_author_name}, Is deleted: {is_author_deleted}, Is suspended: {is_author_suspended}, Author ID: {author_id}") if debugmode else None

                if not (is_author_deleted or is_author_suspended):
                    try:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Account created: {datetime.fromtimestamp(post.author.created_utc)}, Comment Karma: {post.author.comment_karma}, Link Karma: {post.author.link_karma}") if verbosemode else None
                    except AttributeError:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Some account attributes not available") if verbosemode else None

                    # Additional check: try to fetch a recent comment
                    try:
                        async for comment in post.author.comments.new(limit=1):
                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Latest comment timestamp: {datetime.fromtimestamp(comment.created_utc)}") if verbosemode else None
                            break
                    except Exception as e:
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Error fetching recent comment: {str(e)}") if verbosemode else None
                else:
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Note: Most attributes are not available for deleted or suspended accounts.") if debugmode else None
            else:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: No author found for post ID: {disp_submission_id}") if debugmode else None

            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Flair GUID {disp_flair_guid} detected on ID: {disp_submission_id} on post '{post.title}' by {disp_author_name} in {disp_subreddit_displayname}") if debugmode else None

        except (asyncprawcore.exceptions.NotFound, asyncprawcore.exceptions.Forbidden) as e:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Exception {type(e).__name__}: Error loading post or author data for ID: {disp_submission_id}. Post may be removed or author may be shadowbanned/deleted. Error details: {str(e)}") if debugmode else None

            await handle_remove_action(post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname)
            await handle_lock_action(post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname)

            #actions_to_complete = ['comment', 'approve', 'remove', 'lock', 'modlogReason', 'ban', 'unban', 'userFlair', 'usernote', 'contributor', 'sendToWebhook']
            actions_to_complete = ['comment', 'approve', 'modlogReason', 'ban', 'unban', 'userFlair', 'usernote', 'contributor', 'sendToWebhook']
            #for action in actions_to_complete:
            #    mark_action_as_completed(submission_id, action)
            #    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Marked action '{action}' as completed for submission {disp_submission_id}") if debugmode else None
            for action in actions_to_complete:
                if action in flair_details:
                    should_mark_completed = False
                    if action == 'comment':
                        should_mark_completed = flair_details[action].get('enabled', False)
                    elif action in ['userFlair', 'ban']:
                        should_mark_completed = flair_details[action].get('enabled', False)
                    else:
                        should_mark_completed = bool(flair_details[action])

                    if should_mark_completed:
                        mark_action_as_completed(submission_id, action)
                        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Marked action '{action}' as completed for deleted post {disp_submission_id}") if debugmode else None

            # Delete all actions for this submission from the database
            delete_completed_actions(submission_id)
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Deleted all actions for problematic post {disp_submission_id} from database") if debugmode else None
            return

        except Exception as e:
            error_message = f"reddit_error_handler:\n Function: process_flair_assignment\n Unexpected Error: {str(e)}\n Traceback: {traceback.format_exc()}"
            print(error_message) if debugmode else None
            await error_handler(error_message, notify_discord=True)
            return

        """
        try:
            await post.load()

            # Load subreddit data
            try:
                await subreddit.load()
                subreddit_id = subreddit.id
            except AttributeError:
                user_info += f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Unable to fetch subreddit ID for {subreddit.display_name}\n" if debugmode else ""

            disp_author_name = get_display_name(post.author, colored_console_output)

            if post.author:
                is_author_deleted = False
                post_author_name = post.author.name
                await post.author.load()
                is_author_suspended = hasattr(post.author, 'is_suspended') and post.author.is_suspended
                author_id = None if is_author_suspended else getattr(post.author, 'id', None)

                author_details += f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Author Info:\n"
                author_details += f"                         Username: {post_author_name}  |  Is deleted: {is_author_deleted}  |  Is suspended: {is_author_suspended}  |  Author ID: {author_id}\n"

                if not (is_author_deleted or is_author_suspended):
                    try:
                        author_details += f"                         Account created: {datetime.fromtimestamp(post.author.created_utc)}  |  Comment Karma: {post.author.comment_karma}  |  Link Karma: {post.author.link_karma}\n" if verbosemode else ""
                    except AttributeError:
                        author_details += "                         Some account attributes not available\n" if verbosemode else ""

                    # Additional check: try to fetch a recent comment
                    try:
                        async for comment in post.author.comments.new(limit=1):
                            author_details += f"                         Latest comment timestamp: {datetime.fromtimestamp(comment.created_utc)}\n" if verbosemode else ""
                            break
                    except Exception as e:
                        author_details += f"                         Error fetching recent comment: {str(e)}\n" if verbosemode else ""
                else:
                    author_details += "                         Note: Most attributes are not available for deleted or suspended accounts.\n" if debugmode else ""
            else:
                user_info += f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: No author found for post ID: {disp_submission_id}\n" if debugmode else ""

            # Now print all the collected information
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Flair GUID {disp_flair_guid} detected on ID: {disp_submission_id} on post '{post.title}' by {disp_author_name} in {disp_subreddit_displayname}") if debugmode else None


        except (asyncprawcore.exceptions.NotFound, asyncprawcore.exceptions.Forbidden) as e:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Exception NotFound or Forbidden: Error loading post or author data for ID: {disp_submission_id}. Post may be removed or author may be shadowbanned/deleted. Skipping flair assignment.") if debugmode else None
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

        except Exception as e:
            error_message = f"reddit_error_handler:\n Function: process_flair_assignment\n Unexpected Error: {str(e)}"
            print(error_message)
            print(traceback.format_exc())
            await error_handler(error_message, notify_discord=True)
            return
            # Handle the error appropriately
        """

        is_author_deleted_or_suspended = is_author_deleted or is_author_suspended

        # Initialize defaults if the user has no current flair
        flair_text = ''
        flair_css_class = ''

        if not is_author_deleted_or_suspended and post.author:
            try:
                current_flair = await fetch_user_flair(subreddit, post.author.name)
                if current_flair:
                    flair_text = current_flair.get('flair_text', '')
                    flair_css_class = current_flair.get('flair_css_class', '')
            except Exception as e:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Error fetching current flair: {str(e)}") if debugmode else None

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
            'author_id': author_id if author_id is not None else '[deleted]',
            'subreddit_id': subreddit_id if subreddit_id else '[unavailable]'
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

        try:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Beginning action processing for submission {disp_submission_id}") if debugmode else None

            # Handle each action
            if not is_action_completed(submission_id, 'approve') and 'approve' in flair_details and flair_details['approve']:
                await handle_approve_action(post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname)

            if not is_action_completed(submission_id, 'remove') and 'remove' in flair_details and flair_details['remove']:
                await handle_remove_action(post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname)

            if not flair_details.get('remove') and not is_action_completed(submission_id, 'modlogReason') and flair_details.get('modlogReason'):
                await handle_modlog_reason_action(post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname)

            if not is_action_completed(submission_id, 'lock') and 'lock' in flair_details and flair_details['lock']:
                await handle_lock_action(post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname)

            if not is_action_completed(submission_id, 'spoiler') and 'spoiler' in flair_details and flair_details['spoiler']:
                await handle_spoiler_action(post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname)

            if not is_action_completed(submission_id, 'clearPostFlair') and 'clearPostFlair' in flair_details and flair_details['clearPostFlair']:
                await handle_clear_post_flair_action(post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname)

            if not is_action_completed(submission_id, 'sendToWebhook') and 'sendToWebhook' in flair_details and flair_details['sendToWebhook']:
                await handle_webhook_action(config, post, flair_text, mod_name, flair_guid, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname)

            if not (is_author_deleted or is_author_suspended):
                if not is_action_completed(submission_id, 'comment') and 'comment' in flair_details and flair_details['comment']['enabled']:
                    await handle_comment_action(post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname, config, formatted_removal_reason_comment)

                if not is_action_completed(submission_id, 'ban') and 'ban' in flair_details and flair_details['ban']['enabled']:
                    await handle_ban_action(subreddit, post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname, placeholders, mod_name)

                if not is_action_completed(submission_id, 'unban') and 'unban' in flair_details and flair_details['unban']:
                    await handle_unban_action(subreddit, post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname)

                if not is_action_completed(submission_id, 'userFlair') and 'userFlair' in flair_details and flair_details['userFlair']['enabled']:
                    await handle_user_flair_action(subreddit, post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname, placeholders)

                if not is_action_completed(submission_id, 'usernote') and 'usernote' in flair_details and flair_details['usernote']['enabled']:
                    await handle_usernote_action(subreddit, post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname, placeholders, config, mod_name)

                if not is_action_completed(submission_id, 'contributor') and 'contributor' in flair_details and flair_details['contributor']['enabled']:
                    await handle_contributor_action(subreddit, post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname)

                if not is_action_completed(submission_id, 'nuke') and 'nuke' in flair_details and flair_details['nuke'].get('enabled', False):
                    if allow_ban_and_nuke:
                        await handle_nuke_action(reddit, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname, post)
                    else:
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
                await handle_nuke_user_comments_action(post, submission_id, flair_details, disp_submission_id, disp_subreddit_displayname)

        except Exception as e:
            await error_handler(f"Error in process_flair_assignment for {disp_submission_id}: {str(e)}", notify_discord=True)

        #finally:
        #    if is_submission_completed(submission_id):
        #        delete_completed_actions(submission_id)
        #        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: All actions for submission {disp_submission_id} completed and deleted from the database") if debugmode else None







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
                                        flair_notes = flair_details.get('notes', 'No description')  # Get the notes, or 'No description' if not available

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
                                            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Actions for flair GUID {disp_flair_guid} ('{flair_notes}')") if debugmode else None
                                            print(f"                         under submission {disp_submission_id} in {disp_subreddit_displayname} added to the database") if debugmode else None
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



async def process_flair_actions(reddit, max_concurrency=2, processing_retry_delay=3, retry_delay=15):
    semaphore = asyncio.Semaphore(max_concurrency)
    retry_tracker = defaultdict(lambda: {"attempts": 0, "last_attempt": None})

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

                # Reset retry tracker on success
                retry_tracker.pop(submission_id, None)

            except Exception as e:
                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Error processing actions for submission {disp_submission_id}: {str(e)}") if debugmode else None

                retry_tracker[submission_id]["attempts"] += 1
                retry_tracker[submission_id]["last_attempt"] = datetime.utcnow()

                if retry_tracker[submission_id]["attempts"] >= processing_retry_delay:
                    await send_failure_notification(submission_id, mod_name, str(e))
                    mark_all_actions_completed(submission_id)
                    retry_tracker.pop(submission_id, None)
                else:
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Retry {retry_tracker[submission_id]['attempts']} for submission {disp_submission_id}") if debugmode else None

    while True:
        pending_submission_ids = get_pending_submission_ids_from_database()

        if pending_submission_ids:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Found {len(pending_submission_ids)} pending submissions") if debugmode else None

            tasks = []
            for submission_id, mod_name in pending_submission_ids:
                # Check if all actions for the submission are completed
                if is_submission_completed(submission_id):
                    delete_completed_actions(submission_id)
                    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: All actions for submission {submission_id} completed. Skipping processing.") if debugmode else None
                    continue

                # Check if we need to wait before retrying
                if submission_id in retry_tracker:
                    last_attempt = retry_tracker[submission_id]["last_attempt"]
                    if datetime.utcnow() - last_attempt < timedelta(seconds=retry_delay):
                        continue

                if colored_console_output:
                    disp_modname = colored(mod_name, "green", attrs=["underline", "bold"])
                else:
                    disp_modname = mod_name

                print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Pending Actions on Submission {submission_id} actioned by {disp_modname} found in database") if debugmode else None
                task = asyncio.create_task(process_flair_assignment_with_semaphore(submission_id, mod_name))
                tasks.append(task)

            if tasks:
                await asyncio.gather(*tasks)

        await asyncio.sleep(1)  # Always sleep for 1 second between checks



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






async def start_process_flair_actions_task(reddit, max_concurrency, max_processing_retries, processing_retry_delay):
    while True:
        try:
            await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [Start Task] Flair Action Monitoring Started.")
            await process_flair_actions(reddit, max_concurrency, max_processing_retries, processing_retry_delay)
        except Exception as e:
            #print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [Start Modmail Task] Error in modmail task: {str(e)}\nWaiting 60 seconds before restarting monitor_modmail_stream") if debugmode else None
            await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [Start Task] Error in process_flair_actions task: {str(e)}\nWaiting 20 seconds before restarting monitor_modmail_stream")
            await asyncio.sleep(20)
            await add_task('Flair Helper - Process Flair Actions', start_task, start_process_flair_actions_task, reddit, max_concurrency, max_processing_retries, processing_retry_delay)
            return


async def start_monitor_mod_log_task(reddit, bot_username):
    while True:
        try:
            await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [Start Task] Mod Log Monitoring Started.")
            await monitor_mod_log(reddit, bot_username)
        except Exception as e:
            #print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [Start Modqueue Task] Error in modqueue task: {str(e)}\nWaiting 60 seconds before restarting monitor_mod_queue") if debugmode else None
            await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [Start Task] Error in monitor_mod_log task: {str(e)}\nWaiting 20 seconds before restarting monitor_mod_log")
            await asyncio.sleep(20)
            await add_task('Reddit - Monitor Mod Log', start_task, start_monitor_mod_log_task, reddit, bot_username)
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
            await add_task('Reddit - Monitor Private Messages', start_task, start_monitor_private_messages_task, reddit)
            return



async def robust_bot_polling(bot):
    initial_retry_delay = 1
    max_retry_delay = 60
    retry_delay = initial_retry_delay

    while True:
        try:
            print("Starting Telebot polling...") if debugmode or verbosemode else None
            await telegram_bot.polling(non_stop=True, timeout=60)
        except ApiTelegramException as e:
            if e.error_code == 409:
                print("Conflict error: Another instance of the bot is running.") if debugmode else None
                await asyncio.sleep(10)
            else:
                print(f"ApiTelegramException: {e}") if debugmode else None
                await asyncio.sleep(retry_delay)
        except Exception as e:
            print(f"Error in Telebot polling: {e}") if debugmode else None
            await asyncio.sleep(retry_delay)

        retry_delay = min(retry_delay * 2, max_retry_delay)
        print(f"Restarting Telebot polling in {retry_delay} seconds...") if debugmode else None



last_startup_time_main = None

running_tasks: Dict[str, asyncio.Task] = {}

async def add_task(task_name: str, task_func: Callable, *args: Any) -> None:
    global running_tasks

    async def wrapped_task():
        while True:
            try:
                await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [add_task Task Started] {task_name}")
                await task_func(*args)
            except Exception as e:
                error_message = f"Error in {task_name}: {str(e)}"
                await error_handler(error_message, notify_discord=True)
                await asyncio.sleep(20)  # Wait before restarting
            finally:
                await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [add_task Task Stopped] {task_name} - Restarting...")

    try:
        if task_name in running_tasks:
            await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [add_task Task Cancelling] {task_name}")
            running_tasks[task_name].cancel()
            try:
                await running_tasks[task_name]
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(2)  # Wait for the task to be cancelled

        task = asyncio.create_task(wrapped_task())
        running_tasks[task_name] = task
        await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [add_task Task Created] {task_name}")
    except Exception as e:
        await error_handler(f"Error in add_task for {task_name}: {str(e)}", notify_discord=True)



async def start_task(task_func, *args, task_name=None):
    initial_delay = 10
    max_delay = 160  # Assuming this was defined elsewhere
    max_retries = 5
    delay = initial_delay

    task_name = task_name or task_func.__name__

    retries = 0
    while retries < max_retries:
        try:
            await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [start_task Started] {task_name}")
            await task_func(*args)
            # Task completed successfully, break out of the loop
            await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [start_task Task Completed] {task_name}")
            break
        except Exception as e:
            retries += 1
            error_message = f"[start_task] Error in {task_name}: {str(e)}. Retry {retries}/{max_retries} in {delay} seconds..."
            await error_handler(error_message, notify_discord=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)  # Double the delay, capped at max_delay
        finally:
            await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: [start_task Task Stopped] {task_name} - {'Restarting...' if retries < max_retries else 'Max retries reached.'}")

    else:
        # Max retries reached, handle the error
        await error_handler(f"[start_task] Max retries reached for {task_name}. Exiting task.", notify_discord=True)

    # Reset delay for next run
    delay = initial_delay


telegram_bot_id = None  # initialize as a global variable for Telegram telegram_bot_id


@reddit_error_handler
async def bot_main():
    global telegram_bot_id, running_tasks, reddit, bot_username, max_concurrency, max_processing_retries, processing_retry_delay

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

    '''
    if config.telegram_bot_control:
        try:
            print("Connecting to Telegram servers...") if debugmode or verbosemode else None
            telegram_bot_id = (await telegram_bot.get_me()).id
            await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Successfully connected to Telegram servers. Bot ID: {telegram_bot_id}")
            # Replace the old bot_polling_task with the new robust_bot_polling
            await add_task('robust_bot_polling', start_task, robust_bot_polling, bot)
        except Exception as e:
            await error_handler(f"Critical error in bot_main: {str(e)}")
            await asyncio.sleep(10)
    '''

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

    wiki_fetch_delay = 90

    max_concurrency = 2
    max_processing_retries = 3
    processing_retry_delay = 15

    try:
        if config.telegram_bot_control:
            print("Connecting to Telegram servers...") if debugmode or verbosemode else None
            telegram_bot_id = (await telegram_bot.get_me()).id
            await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Successfully connected to Telegram servers. Bot ID: {telegram_bot_id}")
            # Replace the old bot_polling_task with the new robust_bot_polling
            await add_task('Telegram - Bot Polling', start_task, robust_bot_polling, telegram_bot)

        await add_task('Flair Helper - Process Flair Actions', start_task, start_process_flair_actions_task, reddit, max_concurrency, max_processing_retries, processing_retry_delay)
        await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: {action_type}Starting Process Flair Actions...")

        await add_task('Reddit - Monitor Mod Log', start_task, start_monitor_mod_log_task, reddit, bot_username)
        await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: {action_type}Starting Monitor Mod Log...")

        await add_task('Reddit - Monitor Private Messages', start_task, start_monitor_private_messages_task, reddit)
        await discord_status_notification(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: {action_type}Starting Monitor Private Messages...")

        await delayed_fetch_and_cache_configs(reddit, bot_username, wiki_fetch_delay)

        await asyncio.gather(*running_tasks.values())
    except asyncio.CancelledError:
        print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: {action_type}Tasks cancelled") if debugmode else None
    except Exception as e:
        await error_handler(f"Critical error in bot_main: {str(e)}")
        await asyncio.sleep(10)
    finally:
        for task in running_tasks.values():
            task.cancel()
        await asyncio.gather(*running_tasks.values(), return_exceptions=True)

def main():
    loop = asyncio.get_event_loop()
    asyncio.ensure_future(bot_main())
    loop.run_forever()

if __name__ == "__main__":
    #asyncio.run(main())
    #asyncio.get_event_loop().run_until_complete(bot_main())
    main()
