from telegram.ext import Updater, CommandHandler, MessageHandler, Filters
from telegram import Bot
from decouple import config
import os

"""Importing Token from .env file."""
TOKEN = config("TOKEN")

def start(update, context):
    """Response for /start ."""
    update.message.reply_text('Hello! Send me a video file.')

def help(update, context):
    """Response for /help ."""
    update.message.reply_text('You can send me video files in document format under 20mb, and i will give streamable format.  credits: https://t.me/AmirMohamadAminY \n thank to Mr Hosein Moghaddam')

def handle_video(update, context):
    """Handle video files."""
    video_file = update.message.document
    new_file = context.bot.get_file(video_file.file_id)
    file_path = f"{video_file.file_id}.mp4"
    new_file.download(file_path)

    with open(file_path, 'rb') as video:
        update.message.reply_video(video)

    # Delete the file after sending
    os.remove(file_path)

def main():
    # Create the Updater and pass it your bot's token.
    updater = Updater(TOKEN, use_context=True)

    # Get the dispatcher to register handlers
    dp = updater.dispatcher

    # on different commands - answer in Telegram
    dp.add_handler(CommandHandler("start", start))

    # on different commands - answer in Telegram
    dp.add_handler(CommandHandler("help", help))

    # Handle video files
    dp.add_handler(MessageHandler(Filters.document.mime_type("video/mp4"), handle_video))

    updater.start_polling()

    # Run the bot until you press Ctrl-C on server
    updater.idle()

if __name__ == '__main__':
    main()