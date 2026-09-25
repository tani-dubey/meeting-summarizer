import discord
from discord import app_commands
from discord.ext import commands, voice_recv
import logging
import os
import sys
import asyncio
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

from meeting_state import MeetingStateManager, MeetingStatus
from audio_recorder import AudioRecordingManager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot.log')
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()


class MeetingBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.guilds = True
        
        super().__init__(
            command_prefix="!",  # Not used, but required
            intents=intents,
            help_command=None
        )
        
        self.meeting_manager = MeetingStateManager()
        self.audio_manager = AudioRecordingManager(recordings_dir="recordings")
        
        # Reconnection configuration
        self.max_reconnect_attempts = 3
        self.reconnect_base_delay = 2  # seconds
        self._reconnecting = {}  # Track reconnection state per guild
        
    async def setup_hook(self):
        logger.info("Setting up bot...")
        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} command(s)")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")
    
    async def on_ready(self):
        logger.info(f"Bot ready! Logged in as {self.user} (ID: {self.user.id})")
        logger.info(f"Connected to {len(self.guilds)} guild(s)")
        
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState
    ):
        # Detect bot disconnection from voice channel
        if member.id == self.user.id and before.channel and not after.channel:
            guild_id = member.guild.id
            logger.warning(f"Bot was disconnected from voice channel in guild {guild_id}")
            
            if self.meeting_manager.is_meeting_active(guild_id):
                # Prevent multiple concurrent reconnection attempts
                if guild_id in self._reconnecting:
                    logger.info(f"Reconnection already in progress for guild {guild_id}")
                    return
                
                session = self.meeting_manager.get_active_meeting(guild_id)
                logger.info(f"Attempting to reconnect for meeting {session.meeting_id}")
                
                # Mark as reconnecting
                self._reconnecting[guild_id] = True
                
                # Attempt reconnection
                reconnected = await self._attempt_reconnect(member.guild, session)
                
                # Clean up reconnecting flag
                if guild_id in self._reconnecting:
                    del self._reconnecting[guild_id]
                
                if not reconnected:
                    logger.error(f"Failed to reconnect after all attempts. Ending meeting {session.meeting_id}")
                    try:
                        # Final cleanup
                        if member.guild.voice_client:
                            await self.audio_manager.stop_recording(
                                guild_id,
                                member.guild.voice_client
                            )
                        self.meeting_manager.end_meeting(guild_id)
                        
                        # Notify in text channel if possible
                        text_channel = member.guild.get_channel(session.text_channel_id)
                        if text_channel:
                            await text_channel.send(
                                f"⚠️ **Meeting ended due to connection failure**\n"
                                f"Meeting ID: `{session.meeting_id[:8]}`\n"
                                f"The bot was disconnected and could not reconnect after {self.max_reconnect_attempts} attempts."
                            )
                    except Exception as e:
                        logger.error(f"Error during final cleanup: {e}")

    async def _attempt_reconnect(
        self,
        guild: discord.Guild,
        session
    ) -> bool:
        """
        Attempt to reconnect to voice channel with exponential backoff.
        Returns True if reconnection successful, False otherwise.
        """
        guild_id = guild.id
        voice_channel = guild.get_channel(session.voice_channel_id)
        
        if not voice_channel:
            logger.error(f"Voice channel {session.voice_channel_id} no longer exists")
            return False
        
        for attempt in range(1, self.max_reconnect_attempts + 1):
            try:
                logger.info(f"Reconnection attempt {attempt}/{self.max_reconnect_attempts} for guild {guild_id}")
                
                # Wait with exponential backoff
                if attempt > 1:
                    delay = self.reconnect_base_delay * (2 ** (attempt - 2))
                    logger.info(f"Waiting {delay}s before reconnection attempt {attempt}")
                    await asyncio.sleep(delay)
                
                # Attempt to reconnect
                voice_client = await voice_channel.connect(
                    cls=voice_recv.VoiceRecvClient
                )
                logger.info(f"Successfully reconnected to voice channel {voice_channel.id} in guild {guild_id}")
                
                # Resume audio recording
                rec_success, rec_error = self.audio_manager.start_recording(
                    guild_id=guild_id,
                    voice_client=voice_client,
                    meeting_id=session.meeting_id
                )
                
                if rec_success:
                    logger.info(f"Successfully resumed recording for meeting {session.meeting_id}")
                    
                    # Notify in text channel
                    text_channel = guild.get_channel(session.text_channel_id)
                    if text_channel:
                        await text_channel.send(
                            f"✅ **Reconnected successfully**\n"
                            f"Meeting ID: `{session.meeting_id[:8]}`\n"
                            f"Recording resumed after temporary disconnection."
                        )
                    
                    return True
                else:
                    logger.error(f"Reconnected but failed to resume recording: {rec_error}")
                    await voice_client.disconnect(force=False)
                    return False
                    
            except discord.ClientException as e:
                logger.warning(f"Reconnection attempt {attempt} failed (ClientException): {e}")
            except Exception as e:
                logger.warning(f"Reconnection attempt {attempt} failed: {e}")
            
            # If this was the last attempt, return failure
            if attempt == self.max_reconnect_attempts:
                logger.error(f"All {self.max_reconnect_attempts} reconnection attempts failed")
                return False
        
        return False


bot = MeetingBot()


@bot.tree.command(name="start-meeting", description="Join voice channel and start recording")
async def start_meeting(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    
    guild_id = interaction.guild.id
    user = interaction.user
    if not isinstance(user, discord.Member) or not user.voice:
        await interaction.followup.send(
            "❌ You must be in a voice channel to start a meeting.",
            ephemeral=True
        )
        return
    
    voice_channel = user.voice.channel
    if bot.meeting_manager.is_meeting_active(guild_id):
        existing = bot.meeting_manager.get_active_meeting(guild_id)
        await interaction.followup.send(
            f"❌ A meeting is already in progress (ID: `{existing.meeting_id[:8]}`)\n"
            f"Use `/end-meeting` to stop it first.",
            ephemeral=True
        )
        return
    
    try:
        success, session, error = bot.meeting_manager.start_meeting(
            guild_id=guild_id,
            voice_channel_id=voice_channel.id,
            text_channel_id=interaction.channel.id,
            initiator_id=user.id
        )
        
        if not success:
            await interaction.followup.send(f"❌ Failed to start meeting: {error}", ephemeral=True)
            return
        
        try:
            voice_client = await voice_channel.connect(
                cls=voice_recv.VoiceRecvClient
            )
            logger.info(f"Connected to voice channel {voice_channel.id} in guild {guild_id}")
        except discord.ClientException as e:
            voice_client = interaction.guild.voice_client
            if not voice_client:
                bot.meeting_manager.end_meeting(guild_id)
                await interaction.followup.send("❌ Failed to connect to voice channel", ephemeral=True)
                return
        except Exception as e:
            logger.error(f"Voice connection error: {e}")
            bot.meeting_manager.end_meeting(guild_id)
            await interaction.followup.send(
                f"❌ Failed to connect to voice: {str(e)}",
                ephemeral=True
            )
            return
        

        rec_success, rec_error = bot.audio_manager.start_recording(
            guild_id=guild_id,
            voice_client=voice_client,
            meeting_id=session.meeting_id
        )
        
        if not rec_success:
            await voice_client.disconnect(force=False)
            bot.meeting_manager.end_meeting(guild_id)
            await interaction.followup.send(
                f"❌ Failed to start recording: {rec_error}",
                ephemeral=True
            )
            return
        
        await interaction.followup.send(
            f"✅ **Meeting started!**\n"
            f"📍 Voice Channel: {voice_channel.mention}\n"
            f"🆔 Meeting ID: `{session.meeting_id[:8]}`\n"
            f"🎙️ Recording in progress...\n\n"
            f"Use `/end-meeting` to stop recording."
        )
        
        logger.info(
            f"Meeting started: {session.meeting_id} in guild {guild_id} "
            f"by user {user.name} ({user.id})"
        )
        
    except Exception as e:
        logger.error(f"Unexpected error in start-meeting: {e}", exc_info=True)
        await interaction.followup.send(
            "❌ An unexpected error occurred. Please try again.",
            ephemeral=True
        )


@bot.tree.command(name="end-meeting", description="Stop recording and leave voice channel")
async def end_meeting(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    
    guild_id = interaction.guild.id
    if not bot.meeting_manager.is_meeting_active(guild_id):
        await interaction.followup.send(
            "❌ No active meeting to end.",
            ephemeral=True
        )
        return
    
    session = bot.meeting_manager.get_active_meeting(guild_id)
    voice_client = interaction.guild.voice_client
    
    if not voice_client:
        bot.meeting_manager.end_meeting(guild_id)
        await interaction.followup.send(
            "⚠️ Meeting state cleaned up (bot was not in voice channel).",
            ephemeral=True
        )
        return
    
    try:
        rec_success, rec_error, recording_info = await bot.audio_manager.stop_recording(
            guild_id=guild_id,
            voice_client=voice_client
        )
        
        if not rec_success:
            logger.error(f"Failed to stop recording: {rec_error}")
        
        await voice_client.disconnect(force=False)
        logger.info(f"Disconnected from voice in guild {guild_id}")
        
        success, ended_session, error = bot.meeting_manager.end_meeting(guild_id)
        
        if not success:
            await interaction.followup.send(f"⚠️ {error}", ephemeral=True)
            return
        
        duration = ended_session.duration_seconds()
        minutes = int(duration // 60)
        seconds = int(duration % 60)
        
        response = (
            f"✅ **Meeting ended!**\n"
            f"🆔 Meeting ID: `{ended_session.meeting_id[:8]}`\n"
            f"⏱️ Duration: {minutes}m {seconds}s\n"
        )
        
        if recording_info:
            response += f"👥 Recorded {recording_info['user_count']} user(s)\n"
            response += f"📁 Saved to: `{recording_info['output_dir']}`"
        
        await interaction.followup.send(response)
        
        logger.info(
            f"Meeting ended: {ended_session.meeting_id} in guild {guild_id}, "
            f"duration: {duration:.1f}s"
        )
        
    except Exception as e:
        logger.error(f"Error in end-meeting: {e}", exc_info=True)
        
        try:
            if voice_client and voice_client.is_connected():
                await voice_client.disconnect(force=True)
            bot.meeting_manager.end_meeting(guild_id)
        except:
            pass
        
        await interaction.followup.send(
            "⚠️ Meeting ended with errors. State has been cleaned up.",
            ephemeral=True
        )


def main():
    token = os.getenv("DISCORD_BOT_TOKEN")
    
    if not token:
        logger.error("DISCORD_BOT_TOKEN not found in environment variables")
        logger.error("Please create a .env file with your bot token")
        sys.exit(1)
    
    Path("recordings").mkdir(exist_ok=True)
    
    try:
        logger.info("Starting bot...")
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        logger.error("Invalid bot token")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
