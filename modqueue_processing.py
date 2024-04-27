import asyncio
import asyncpraw
import asyncprawcore
from asyncprawcore import exceptions as asyncprawcore_exceptions
import traceback
from datetime import datetime
from flair_helper2_async_json import error_handler
from flair_helper2_async_json import reddit_error_handler

verbosemode = False
debugmode = False

async def get_moderated_subreddits(reddit, bot_username):
    moderated_subreddits = []
    async for subreddit in reddit.user.moderator_subreddits():
        if f"u_{bot_username}" not in subreddit.display_name:
            moderated_subreddits.append(subreddit.display_name)
    return moderated_subreddits

async def monitor_mod_queue(reddit, bot_username, update_interval=3600):
    while True:
        try:
            moderated_subreddits = await get_moderated_subreddits(reddit, bot_username)
            subreddit = await reddit.subreddit("+".join(moderated_subreddits))
            mod_queue = subreddit.mod.modqueue(limit=None)
            processed_items = set()

            start_time = datetime.utcnow()
            while (datetime.utcnow() - start_time).total_seconds() < update_interval:
                async for item in mod_queue:
                    if item.id not in processed_items:
                        processed_items.add(item.id)

                        if item.author is None or item.author.name == "[deleted]":
                            await process_ban_evasion_item(item, bot_username)

                await asyncio.sleep(60)  # Wait for 60 seconds before checking for new items

        except (asyncprawcore.exceptions.RequestException, asyncprawcore.exceptions.ResponseException) as e:
            print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Error in mod queue monitoring: {str(e)}. Retrying...") if debugmode else None
            if debugmode:
                traceback.print_exc()
            await asyncio.sleep(30)  # Wait for a short interval before retrying

async def process_ban_evasion_item(item, bot_username):
    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}: Removing [deleted] item from modqueue: {item.permalink}") if debugmode else None
    await item.mod.remove()  # Remove the item
