# Flair Helper 2 config.py

debugmode = False
verbosemode = False

telegram_bot_control = False #Change to True to enable Telegram Bot Functionality
telegram_TOKEN = 'YourBot:TokenIDHere'
telegram_admin_ids = [123456789, 98765421] #ID's of Telegram Users you want to allow interactions with your bot.  If they aren't on this list, interactions with the bot will be ignored.

colored_console_output = True #Requires https://pypi.org/project/termcolor

auto_accept_mod_invites = False

allow_ban_and_nuke = False

# Config Validation Errors are always PM'ed regardless of being True or False
send_pm_on_wiki_config_update = True

discord_bot_notifications = False
discord_webhook_url = "https://discord.com/api/webhooks/YOUR_DISCORD_WEBHOOK"

logs_dir = "logs/"
