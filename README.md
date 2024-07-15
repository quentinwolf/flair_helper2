# Flair Helper 2

Flair Helper 2 is an enhanced version of the original Flair Helper bot for Reddit, designed to fill in the gaps left by the abandoned Flair Helper project and enable others to contribute to provide additional features and further improvements. This bot is a drop-in replacement and is fully backwards compatible with anyone's prior/existing Flair Helper configuration. It is built using Python and the PRAW (Python Reddit API Wrapper) library.

## Features

- Automatically applies predefined actions based on link flair assignments
- Supports various actions such as banning, unbanning (**NEW!**), post removal, post approval (**NEW!** automatically unlocks and unspoilers), commenting, locking, spoilering (**NEW!**), nuke comments (**NEW!**), nuke user (**NEW!**), clear post flair, adding usernotes, discord webhooks and more
- Allows customization of removal reasons, comments, and ban messages using placeholders
- Integrates with Toolbox usernotes for enhanced moderation
- Anonymizes removal comments by default using the subreddit's ModTeam account
- Provides a command to list mod-only flair templates and their corresponding IDs
- Continuously monitors the mod log and incoming private messages for efficient operation
- Detects and reloads configuration changes from the subreddit's wiki page
- Logs errors and important events for debugging and monitoring purposes

## Background

The original Flair Helper bot was a valuable tool for Reddit moderators, but it was abandoned by its maintainer due to Reddit's decision to discontinue support for third-party apps. Flair Helper 2 was created to address the void left by this event and to provide a reliable and feature-rich drop-in replacement. This project aims to offer a robust solution for automating moderation tasks based on link flair assignments.

## Setup and Installation

1. Clone this repository to your local machine:
```
git clone https://github.com/yourusername/FlairHelper2.git
```

2. Install the required dependencies using pip:
```
pip install -r requirements.txt
```

3. Create a new Reddit account for your bot and obtain the necessary API credentials (client ID, client secret, and refresh token) from the Reddit App Preferences.

4. Create a `praw.ini` file in the project directory with the following content:
```
[fh2_login]
client_id=YOUR_CLIENT_ID
client_secret=YOUR_CLIENT_SECRET
refresh_token=YOUR_REFRESH_TOKEN
user_agent=FlairHelper2 by /u/YOUR_REDDIT_USERNAME
```

Replace `YOUR_CLIENT_ID`, `YOUR_CLIENT_SECRET`, `YOUR_REFRESH_TOKEN`, and `YOUR_REDDIT_USERNAME` with your actual credentials.

5. Customize the `flair_helper` wiki page (located at https://www.reddit.com/r/YOURSUBNAME/wiki/flair_helper) to define your desired flair-based actions, removal reasons, comments, and ban messages.  This implementation is 100% backwards compatible with the original flair_helper bot, so if you used the original in the past, this version will look in the same place and parse the same information.

6. Run the bot:
```
python flair_helper2_async.py
```

I initially started off with the non-async version, although found I'd occasionally run into some oddities, so I began converting it over to an asynchronous version.  The old one has mostly been updated with all the same features, although could use further development, and at the current point I've stopped adding features to it as the async version is far superior as it utilizes a single mod log stream to monitor all moderated subs for flair changes vs spawning an individual thread per-sub (which doesn't scale as well if you have more than 15 subs or so).  The async one should technically be good up to a hundred or so subs, depending on how active the subs are due to API limitations of 600 calls every 10 minutes.

## Contributing

Contributions to Flair Helper 2 are welcome! If you have any ideas, suggestions, or bug reports, please open an issue on the GitHub repository.  If you'd like to contribute code improvements, feel free to submit a pull request.

## License

This project is licensed under the [GNU GPLv3 License](LICENSE).

## Acknowledgements

- The original Flair Helper bot for inspiring this project
- The PRAW library for providing a convenient wrapper for the Reddit API
- The Python community for their valuable resources and support

## Contact

If you have any questions or need further assistance, please don't hesitate to reach out to me on Reddit at [/u/quentinwolf](https://www.reddit.com/user/quentinwolf).
