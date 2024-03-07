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

debugmode = True
verbosemode = False

logs_dir = "logs/"
if not os.path.exists(logs_dir):
    os.makedirs(logs_dir)

errors_filename = f'{logs_dir}errors.log'
logging.basicConfig(filename=errors_filename, level=logging.DEBUG, format='%(asctime)s %(levelname)s: %(message)s')
errors_logger = logging.getLogger('errors')

# Create a Reddit instance using PRAW
reddit = praw.Reddit("fh2_login")

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
        return yaml.load(result[0], Loader=yaml.FullLoader)
    return None

def fetch_and_cache_configs(max_retries=2, retry_delay=5, single_sub=None):
    create_database()
    moderated_subreddits = [reddit.subreddit(single_sub)] if single_sub else list(reddit.user.moderator_subreddits())
    bot_username = reddit.user.me().name

    for subreddit in moderated_subreddits:
        subreddit_name = subreddit.display_name
        if f"u_{bot_username}" in subreddit_name:
            print(f"Skipping bot's own user page: r/{subreddit_name}") if debugmode else None
            continue  # Skip the bot's own user page

        retries = 0
        while retries < max_retries:
            try:
                wiki_page = subreddit.wiki['flair_helper']
                wiki_content = wiki_page.content_md.strip()

                if not wiki_content:
                    print(f"Flair Helper configuration for r/{subreddit_name} is blank. Skipping...") if debugmode else None
                    break  # Skip processing if the wiki page is blank

                try:
                    updated_config = yaml.load(wiki_content, Loader=yaml.FullLoader)
                    cached_config = get_cached_config(subreddit_name)

                    if cached_config != updated_config:
                        # Check if the mod who edited the wiki page has the "config" permission
                        if updated_config.get('require_config_to_edit', False):
                            wiki_revision = list(subreddit.wiki['flair_helper'].revisions(limit=1))[0]
                            mod_name = wiki_revision['author']
                            mod = reddit.redditor(mod_name)
                            if not mod.has_permission('config', subreddit=subreddit):
                                error_output = f"Mod {mod_name} does not have permission to edit config in r/{subreddit_name}"
                                print(error_output) if debugmode else None
                                errors_logger.error(error_output)
                                continue  # Skip reloading the configuration

                        try:
                            yaml.load(wiki_content, Loader=yaml.FullLoader)
                            cache_config(subreddit_name, updated_config)
                            print(f"The Flair Helper wiki page configuration for r/{subreddit_name} has been successfully reloaded.") if debugmode else None
                            try:
                                reddit.subreddit(subreddit_name).modmail.create(
                                    subject="Flair Helper Configuration Reloaded",
                                    body="The Flair Helper configuration for r/{} has been successfully reloaded.".format(subreddit_name),
                                    recipient=subreddit
                                )
                            except praw.exceptions.RedditAPIException as e:
                                error_output = f"Error sending modmail to r/{subreddit_name}: {str(e)}"
                                print(error_output) if debugmode else None
                                errors_logger.error(error_output)
                        except yaml.YAMLError as e:
                            error_output = f"Error parsing YAML configuration for r/{subreddit_name}: {str(e)}"
                            print(error_output) if debugmode else None
                            errors_logger.error(error_output)
                            try:
                                reddit.subreddit(subreddit_name).modmail.create(
                                    subject="Flair Helper Configuration Error",
                                    body="The Flair Helper configuration for r/{} could not be reloaded due to YAML parsing errors:\n\n{}".format(subreddit_name, str(e)),
                                    recipient=subreddit
                                )
                            except praw.exceptions.RedditAPIException as e:
                                error_output = f"Error sending modmail to r/{subreddit_name}: {str(e)}"
                                print(error_output) if debugmode else None
                                errors_logger.error(error_output)
                    else:
                        print(f"The Flair Helper wiki page configuration for r/{subreddit_name} has not changed.") if debugmode else None
                    break  # Configuration loaded successfully, exit the retry loop
                except (prawcore.exceptions.ResponseException, prawcore.exceptions.RequestException) as e:
                    error_output = f"Error loading configuration for r/{subreddit_name}: {str(e)}"
                    print(error_output) if debugmode else None
                    errors_logger.error(error_output)
                    retries += 1
                    if retries < max_retries:
                        print(f"Retrying in {retry_delay} seconds...") if debugmode else None
                        time.sleep(retry_delay)
                    else:
                        print(f"Max retries exceeded for r/{subreddit_name}. Skipping...") if debugmode else None
            except prawcore.exceptions.Forbidden:
                error_output = f"Error: Bot does not have permission to access the wiki page in r/{subreddit_name}"
                print(error_output) if debugmode else None
                errors_logger.error(error_output)
                break  # Skip retrying if the bot doesn't have permission
            except prawcore.exceptions.NotFound:
                error_output = f"Flair Helper wiki page doesn't exist for r/{subreddit_name}"
                print(error_output) if debugmode else None
                errors_logger.error(error_output)
                try:
                    reddit.subreddit(subreddit_name).modmail.create(
                        subject="Flair Helper Wiki Page Not Found",
                        body="The Flair Helper wiki page doesn't exist for r/{}. Please go to https://www.reddit.com/r/{}/wiki/flair_helper and create the page to add this subreddit.".format(subreddit_name, subreddit_name),
                        recipient=subreddit
                    )
                except praw.exceptions.RedditAPIException as e:
                    error_output = f"Error sending modmail to r/{subreddit_name}: {str(e)}"
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

def update_usernotes(subreddit, author, note_text, link, mod_name):
    usernotes_wiki = subreddit.wiki['usernotes']
    usernotes_data = json.loads(usernotes_wiki.content_md)

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

# Primary process to handle any flair changes that appear in the logs
def process_flair_assignment(log_entry, config, subreddit):
    target_fullname = log_entry.target_fullname
    if target_fullname.startswith('t3_'):  # Check if it's a submission
        submission_id = target_fullname[3:]  # Remove the 't3_' prefix
        post = reddit.submission(submission_id)
        flair_guid = post.link_flair_template_id
        print(f"Flair GUID detected: {flair_guid}")
        if flair_guid in config['flairs']:
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
                print(f"Mod {log_entry.mod.name} does not have permission to edit config") if debugmode else None
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
            if config['remove'].get(flair_guid, False):
                print(f"remove triggered in r/{subreddit.display_name}") if debugmode else None
                mod_note = config['usernote'].get(flair_guid, '')
                post.mod.remove(spam=False, mod_note=mod_note)


            post_age_days = (datetime.utcnow() - datetime.utcfromtimestamp(post.created_utc)).days

            if config['comment'].get(flair_guid, False):
                max_age = config.get('max_age_for_comment', 175)
                if isinstance(max_age, dict):
                    max_age = max_age.get(flair_guid, 175)
                if post_age_days <= max_age:
                    print(f"comment triggered in r/{subreddit.display_name}") if debugmode else None
                    removal_type = config.get('removal_comment_type', 'public_as_subreddit')
                    post.mod.send_removal_message(message=removal_reason, type=removal_type)

            # Check if banning is configured for the flair GUID
            if 'bans' in config and flair_guid in config['bans']:
                ban_duration = config['bans'][flair_guid]
                ban_message = config['ban_message'].get(flair_guid)
                ban_note = config['ban_note'].get(flair_guid)

                if ban_message:
                    for placeholder, value in placeholders.items():
                        ban_message = ban_message.replace(f"{{{{{placeholder}}}}}", str(value))

                if ban_duration is True:
                    print(f"permanent ban triggered in r/{subreddit.display_name}") if debugmode else None
                    subreddit.banned.add(post.author, ban_message=ban_message, ban_reason=ban_note)
                elif isinstance(ban_duration, int) and ban_duration > 0:
                    print(f"temporary ban triggered for {ban_duration} days in r/{subreddit.display_name}") if debugmode else None
                    subreddit.banned.add(post.author, ban_message=ban_message, ban_reason=ban_note, duration=ban_duration)
                else:
                    print(f"banning not triggered for flair GUID: {flair_guid} in r/{subreddit.display_name}") if debugmode else None


            if config['lock_post'].get(flair_guid, False):
                print(f"lock triggered in r/{subreddit.display_name}") if debugmode else None
                post.mod.lock()

            if config['spoiler_post'].get(flair_guid, False):
                print(f"spoiler triggered in r/{subreddit.display_name}") if debugmode else None
                post.mod.spoiler()

            if config['set_author_flair_text'].get(flair_guid) or config['set_author_flair_css_class'].get(flair_guid):
                print(f"set_author_flair triggered in r/{subreddit.display_name}") if debugmode else None
                current_flair = next(subreddit.flair(post.author.name))
                flair_text = current_flair['flair_text'] if current_flair else ''
                flair_css_class = current_flair['flair_css_class'] if current_flair else ''

                if config['set_author_flair_text'].get(flair_guid):
                    flair_text = config['set_author_flair_text'][flair_guid]

                if config['set_author_flair_css_class'].get(flair_guid):
                    flair_css_class = config['set_author_flair_css_class'][flair_guid]

                subreddit.flair.set(post.author.name, text=flair_text, css_class=flair_css_class)

            if config['usernote'].get(flair_guid):
                print(f"usernote triggered in r/{subreddit.display_name}") if debugmode else None
                author = post.author.name
                note_text = config['usernote'][flair_guid]
                link = post.permalink
                mod_name = log_entry.mod.name

                update_usernotes(subreddit, author, note_text, link, mod_name)

            if 'remove_link_flair' in config and flair_guid in config['remove_link_flair']:
                print(f"remove_link_flair triggered in r/{subreddit.display_name}") if debugmode else None
                post.mod.flair(text='', css_class='')

            if 'add_contributor' in config and flair_guid in config['add_contributor']:
                print(f"add_contributor triggered in r/{subreddit.display_name}") if debugmode else None
                subreddit.contributor.add(post.author)

            if 'remove_contributor' in config and flair_guid in config['remove_contributor']:
                print(f"remove_contributor triggered in r/{subreddit.display_name}") if debugmode else None
                subreddit.contributor.remove(post.author)



# Handle Private Messages to allow the bot to reply back with a list of flairs for convenience
def handle_private_messages():
    for message in reddit.inbox.unread(limit=None):
        if isinstance(message, praw.models.Message):
            subject = message.subject.lower()
            subreddit_name = message.body.strip()

            print(f"PM Received") if debugmode else None

            if subject == 'list':
                print(f"'list' PM Received") if debugmode else None
                try:
                    subreddit = reddit.subreddit(subreddit_name)
                    if subreddit.user_is_moderator:
                        mod_flair_templates = [
                            f"{template['text']}: {template['id']}"
                            for template in subreddit.flair.link_templates
                            if template['mod_only']
                        ]
                        if mod_flair_templates:
                            response = "Mod-only flair templates:\n\n" + "\n\n".join(mod_flair_templates)
                        else:
                            response = "No mod-only flair templates found for r/{}.".format(subreddit_name)
                    else:
                        response = "You are not a moderator of r/{}.".format(subreddit_name)
                except prawcore.exceptions.NotFound:
                    response = "Subreddit r/{} not found.".format(subreddit_name)

            elif subject == 'auto':
                print(f"'auto' PM Received") if debugmode else None
                try:
                    subreddit = reddit.subreddit(subreddit_name)
                    if subreddit.user_is_moderator:
                        use_rules = 'rules' in subreddit_name.lower()

                        if use_rules:
                            rules = list(subreddit.rules)
                            flair_templates = []
                            for rule in rules:
                                flair_templates.append({
                                    'text': rule.short_name,
                                    'id': rule.violation_reason
                                })
                        else:
                            flair_templates = [
                                template for template in subreddit.flair.link_templates
                                if template['mod_only']
                            ]

                        config = {
                            'header': "Hi /u/{{author}}, thanks for contributing to /r/{{subreddit}}. Unfortunately, your post was removed as it violates our rules:",
                            'footer': "Please read the sidebar and the rules of our subreddit [here](https://www.reddit.com/r/{{subreddit}}/about/rules) before posting again. If you have any questions or concerns please [message the moderators through modmail](https://www.reddit.com/message/compose?to=/r/{{subreddit}}&subject=About my removed {{kind}}&message=I'm writing to you about the following {{kind}}: {{url}}. %0D%0DMy issue is...).",
                            'flairs': {},
                            'comment': {},
                            'removal_comment_type': 'public_as_subreddit',
                            'remove': {},
                            'lock_post': {},
                            'spoiler_post': {},
                            'set_author_flair_text': {},
                            'set_author_flair_css_class': {},
                            'usernote': {},
                            'usernote_type_name': 'flair_helper_note'
                        }

                        for template in flair_templates:
                            flair_id = template['id']
                            flair_text = template['text']

                            config['flairs'][flair_id] = f"This is the removal reason for Flair '{flair_text}'"
                            config['comment'][flair_id] = True
                            config['remove'][flair_id] = False
                            config['lock_post'][flair_id] = False
                            config['spoiler_post'][flair_id] = False
                            config['set_author_flair_text'][flair_id] = f"Removed: {flair_text}"
                            config['set_author_flair_css_class'][flair_id] = "removed"
                            config['usernote'][flair_id] = f"Post removed for violating rule: {flair_text}"

                        response = "Here's a sample Flair Helper 2 configuration for your subreddit.\n\nBe sure to click the 'source' button below the message so you can copy the config correctly.\n\nOnly copy the code between the triple \`\`\`'s:\n\n"
                        response += "```\n" + yaml.dump(config, sort_keys=False) + "```"
                    else:
                        response = "You are not a moderator of r/{}.".format(subreddit_name)
                except prawcore.exceptions.NotFound:
                    response = "Subreddit r/{} not found.".format(subreddit_name)

            else:
                response = "Unknown command. Available commands: 'list', 'auto'."

            message.mark_read()
            message.reply(response)



# Primary Mod Log Monitor
def monitor_mod_log(subreddit, config):
    try:
        for log_entry in subreddit.mod.stream.log(skip_existing=True):
            print(f"New log entry: {log_entry.action}") if verbosemode else None
            if log_entry.action == 'editflair':
                print(f"Flair action detected in r/{subreddit.display_name}") if debugmode else None
                if log_entry.target_fullname:
                    process_flair_assignment(log_entry, config, subreddit)
                else:
                    print(f"No target found") if debugmode else None
            elif log_entry.action == 'wikirevise':
                if 'flair_helper' in log_entry.details:
                    print(f"Flair Helper wiki page revised in r/{subreddit.display_name}") if debugmode else None
                    try:
                        fetch_and_cache_configs(max_retries=2, retry_delay=5, single_sub=subreddit.display_name)
                    except prawcore.exceptions.NotFound:
                        error_output = f"Flair Helper wiki page not found in r/{subreddit.display_name}"
                        print(error_output) if debugmode else None
                        errors_logger.error(error_output)
                #else:
                    #print(f"Ignoring wiki revision: {log_entry.details} in r/{subreddit.display_name}") if debugmode else None
            else:
                print(f"Ignoring action: {log_entry.action} in r/{subreddit.display_name}") if verbosemode else None
    except prawcore.exceptions.ResponseException as e:
        print(f"Error: {e}") if debugmode else None

# Create Multithreaded Instance to monitor all subs that have a valid Flair_Helper configuration
def run_bot():
    fetch_and_cache_configs()
    moderated_subreddits = list(reddit.user.moderator_subreddits())
    bot_username = reddit.user.me().name

    with concurrent.futures.ThreadPoolExecutor() as flair_executor:
        flair_futures = []
        for subreddit in moderated_subreddits:
            subreddit_name = subreddit.display_name
            if f"u_{bot_username}" in subreddit_name:
                print(f"Skipping bot's own user page: r/{subreddit_name}") if debugmode else None
                continue  # Skip the bot's own user page

            config = get_cached_config(subreddit_name)

            if config:
                print(f"Monitoring mod log for r/{subreddit_name}")
                future = flair_executor.submit(monitor_mod_log, subreddit, config)
                flair_futures.append(future)
            else:
                print(f"No Flair Helper configuration found for r/{subreddit_name}")

        # Wait for all the flair-related futures to complete
        concurrent.futures.wait(flair_futures)

# Check for PM's every 60 seconds
def monitor_private_messages():
    while True:
        handle_private_messages()
        time.sleep(60)  # Sleep for 60 seconds before the next iteration


def main():
    with concurrent.futures.ThreadPoolExecutor() as executor:
        executor.submit(run_bot)
        executor.submit(monitor_private_messages)

if __name__ == "__main__":
    main()
