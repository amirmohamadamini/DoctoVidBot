import os
import logging
from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeFilename
from telethon.tl.custom import Button
from dotenv import load_dotenv

# logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# environment variables
load_dotenv()
API_ID = int(os.getenv('API_ID'))
API_HASH = os.getenv('API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN')
print(API_ID)
# Telethon client
bot = TelegramClient('video_converter_bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

active_conversions = set()

TEMP_DIR = os.path.join(os.getcwd(), 'temp')
os.makedirs(TEMP_DIR, exist_ok=True)

@bot.on(events.NewMessage(pattern='/start'))
async def start_command(event):
    """Handle /start command"""
    await event.respond(
        "👋 Welcome to Video Converter Bot!\n\n"
        "I can make any video file streamable in Telegram.\n\n"
        "Simply send me any video file, and I'll convert it to a format that can be streamed directly in Telegram.\n\n"
        "💡 Tip: Just send me a video as a document and I'll send it back as a streamable video!",
    )

@bot.on(events.NewMessage(pattern='/help'))
async def help_command(event):
    """Handle /help command"""
    await event.respond(
        "📹 How to use Video Converter Bot:\n\n"
        "1. Send me any video file (as a document)\n"
        "2. Wait for me to process it\n"
        "3. Receive your streamable video!\n\n"
        "That's it! No complicated settings needed."
    )

@bot.on(events.NewMessage(func=lambda e: e.document or e.video))
async def process_video(event):
    """Process video files (either as document or video)"""
    user_id = event.sender_id
    chat_id = event.chat_id
    message_id = event.id

    if user_id in active_conversions:
        await event.respond("⏳ Please wait for your current video to finish processing.")
        return
    
    # Add user to active conversions
    active_conversions.add(user_id)
    
    try:
        # Send processing message
        processing_msg = await event.respond("⏳ Processing your video...")
        
        # Get the file
        if event.document:
            media = event.document
            # Check if it's actually a video file
            filename = "unknown_file"
            is_video = False
            video_duration = 0
            video_width = 0
            video_height = 0
            
            # Try to extract video attributes if available
            for attr in media.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    filename = attr.file_name
                if isinstance(attr, DocumentAttributeVideo):
                    is_video = True
                    video_duration = attr.duration
                    video_width = attr.w
                    video_height = attr.h
            
            # If we couldn't determine from attributes, check extension
            if not is_video:
                video_extensions = ['.mp4', '.avi', '.mkv', '.mov', '.flv', '.wmv', '.webm', '.m4v', '.3gp']
                is_video = any(filename.lower().endswith(ext) for ext in video_extensions)
            
            if not is_video:
                await processing_msg.edit("❌ This doesn't appear to be a video file. Please send a video file.")
                active_conversions.remove(user_id)
                return
        else:
            # It's already a video, just get its attributes
            media = event.video
            video_duration = media.duration
            video_width = media.w
            video_height = media.h
            filename = "video.mp4"
            for attr in media.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    filename = attr.file_name
                    break
        
        temp_file = os.path.join(TEMP_DIR, f"temp_{user_id}_{message_id}_{filename}")
        
        # Download file
        await processing_msg.edit("⬇️ Downloading video...")
        await bot.download_media(message=event.message, file=temp_file)
        
        thumb = None
        if hasattr(event.message.media, 'thumbs') and event.message.media.thumbs:
            await processing_msg.edit("🖼️ Extracting thumbnail...")
            thumb_path = os.path.join(TEMP_DIR, f"thumb_{user_id}_{message_id}.jpg")
            thumb = await bot.download_media(event.message.media.thumbs[0], thumb_path)
        
        if event.video and not event.document:
            await processing_msg.edit("ℹ️ This video is already in streamable format!")
            os.remove(temp_file)
            if thumb and os.path.exists(thumb):
                os.remove(thumb)
            active_conversions.remove(user_id)
            return
        

        await processing_msg.edit("⬆️ Converting to streamable format...")
        
        if not filename.lower().endswith(('.mp4', '.avi', '.mkv', '.mov')):
            filename = f"{filename}.mp4"

        await bot.send_file(
            chat_id,
            temp_file,
            caption="✅ Here's your streamable video!",
            supports_streaming=True,
            video_note=False,
            thumb=thumb,
            attributes=[
                DocumentAttributeVideo(
                    duration=video_duration if video_duration else 0,
                    w=video_width if video_width else 1280,
                    h=video_height if video_height else 720,
                    supports_streaming=True
                )
            ],
            reply_to=message_id,
            file_name=filename
        )

        await processing_msg.delete()
        if os.path.exists(temp_file):
            os.remove(temp_file)
        if thumb and os.path.exists(thumb):
            os.remove(thumb)
            
    except Exception as e:
        logger.error(f"Error: {e}")
        await event.respond(f"❌ An error occurred while processing your video: {str(e)}")
    finally:
        if user_id in active_conversions:
            active_conversions.remove(user_id)

async def cleanup_temp_files():
    """Cleanup temporary files on startup"""
    for filename in os.listdir(TEMP_DIR):
        file_path = os.path.join(TEMP_DIR, filename)
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
        except Exception as e:
            logger.error(f"Error cleaning up file {file_path}: {e}")

def main():
    """Run the bot"""
    print("Starting Video Converter Bot")
    bot.loop.run_until_complete(cleanup_temp_files())
    bot.run_until_disconnected()

if __name__ == '__main__':
    main()
