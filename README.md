# telegram-bot-on-gcp
This Telegram bot is a Google Cloud function. It also uses Google Cloud Datastore. It shows how to use Telegram location services, keyboards, and more.

# Installation
1. Create a telegram bot https://core.telegram.org/bots
2. Deploy this bot to GCP using the gcloud tool (use --trigger-http type). https://cloud.google.com/functions/docs/deploying/filesystem
3. Call the setWebHook method in the Bot API via the following url:
https://api.telegram.org/bot{my_bot_token}/setWebhook?url={url_to_send_updates_to}.
More info here https://core.telegram.org/bots/api#setwebhook
