import discord
import wave
import os
import threading
from pathlib import Path
from typing import Dict, Optional
import logging

from discord.ext import voice_recv

logger = logging.getLogger(__name__)


class AudioSink(voice_recv.AudioSink):
    def __init__(self, meeting_id: str, output_dir: str = "recordings"):
        super().__init__()
        
        self.meeting_id = meeting_id
        self.output_dir = Path(output_dir)
        self.meeting_dir = self.output_dir / meeting_id
        self.meeting_dir.mkdir(parents=True, exist_ok=True)
        self._user_writers: Dict[int, wave.Wave_write] = {}
        self._lock = threading.Lock()
        self.sample_rate = 48000
        self.channels = 2
        self.sample_width = 2
        
        logger.info(f"AudioSink initialized for meeting {meeting_id}")
    
    def wants_opus(self):
        return False
    
    def write(self, user, data):
        if user is None or not data.pcm:
            return 
        
        with self._lock:
            user_id = user.id if hasattr(user, 'id') else user
            
            if user_id not in self._user_writers:
                username = user.name if hasattr(user, 'name') else f"user_{user_id}"
                self._init_user_writer(user_id, username)
            
            try:
                writer = self._user_writers[user_id]
                writer.writeframes(data.pcm)
            except Exception as e:
                logger.error(f"Error writing audio for user {user_id}: {e}")
    
    def _init_user_writer(self, user_id: int, username: str):
        safe_username = "".join(c for c in username if c.isalnum() or c in (' ', '-', '_'))
        filename = f"user_{user_id}_{safe_username}.wav"
        filepath = self.meeting_dir / filename
        
        try:
            writer = wave.open(str(filepath), 'wb')
            writer.setnchannels(self.channels)
            writer.setsampwidth(self.sample_width)
            writer.setframerate(self.sample_rate)
            
            self._user_writers[user_id] = writer
            logger.info(f"Started recording for user {username} ({user_id})")
        except Exception as e:
            logger.error(f"Failed to create audio file for user {user_id}: {e}")
    
    def cleanup(self):
        with self._lock:
            logger.info(f"Cleaning up audio sink for meeting {self.meeting_id}")
            
            for user_id, writer in self._user_writers.items():
                try:
                    writer.close()
                    logger.info(f"Closed audio file for user {user_id}")
                except Exception as e:
                    logger.error(f"Error closing audio file for user {user_id}: {e}")
            
            self._user_writers.clear()
    
    def get_recording_info(self) -> dict:
        with self._lock:
            return {
                "meeting_id": self.meeting_id,
                "output_dir": str(self.meeting_dir),
                "user_count": len(self._user_writers),
                "user_ids": list(self._user_writers.keys())
            }


class AudioRecordingManager:
    def __init__(self, recordings_dir: str = "recordings"):
        self.recordings_dir = recordings_dir
        self._active_recorders: Dict[int, AudioSink] = {}
        
    def start_recording(
        self,
        guild_id: int,
        voice_client: discord.VoiceClient,
        meeting_id: str
    ) -> tuple[bool, Optional[str]]:
        if guild_id in self._active_recorders:
            return (False, "Recording already active for this guild")
        
        try:
            sink = AudioSink(meeting_id, self.recordings_dir)
            
            voice_client.listen(
                sink,
                after = self._recording_error_callback
            )

            self._active_recorders[guild_id] = sink
            logger.info(f"Started recording for guild {guild_id}, meeting {meeting_id}")
            return (True, None)
            
        except Exception as e:
            logger.error(f"Failed to start recording: {e}")
            return (False, f"Failed to start recording: {str(e)}")
    
    async def stop_recording(
        self,
        guild_id: int,
        voice_client: discord.VoiceClient
    ) -> tuple[bool, Optional[str], Optional[dict]]:
        if guild_id not in self._active_recorders:
            return (False, "No active recording for this guild", None)
        
        try:
            sink = self._active_recorders[guild_id]
            
            if hasattr(voice_client, 'stop_listening'):
                voice_client.stop_listening()
            elif hasattr(voice_client, 'stop_recording'):
                voice_client.stop_recording()
            
            recording_info = sink.get_recording_info()
            sink.cleanup()
            
            del self._active_recorders[guild_id]
            logger.info(f"Stopped recording for guild {guild_id}")
            
            return (True, None, recording_info)
            
        except Exception as e:
            logger.error(f"Error stopping recording: {e}")
            return (False, f"Error stopping recording: {str(e)}", None)
    
    def _recording_callback(self, sink: AudioSink, channel):
        logger.info(f"Recording callback triggered for meeting {sink.meeting_id}")
    
    def _recording_error_callback(self, exc: Exception):
        if exc is None:
            logger.info("Recording listener stopped normally")
            return 
        logger.error(f"Recording error : {exc!r}", exc_info=True)
