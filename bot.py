import asyncio
import json
import logging
import os
import re
import time
import traceback
from typing import Optional
from urllib.parse import parse_qs, urlparse

import aiohttp

import discord
from discord import app_commands
from discord.ext import commands

from openai import OpenAI

from dotenv import load_dotenv
import yt_dlp


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('musicologo')

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
COMMAND_PREFIX = os.getenv('COMMAND_PREFIX', '!')

if os.getenv('OPENAI_API_KEY'):
    os.environ['OPENAI_API_KEY'] = os.getenv('OPENAI_API_KEY')
    openai_client = OpenAI()
else:
    openai_client = None

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'extractaudio': True,
    'audioformat': 'mp3',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

MIN_PLAYBACK_SPEED = 0.5
MAX_PLAYBACK_SPEED = 2.0
PLAYBACK_SPEED_TOLERANCE = 0.005


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.69, start_time=0, playback_speed=1.0):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')
        self.duration = data.get('duration')
        self.start_time = start_time
        self.playback_speed = playback_speed

    @classmethod
    async def from_url(
        cls,
        url,
        *,
        loop=None,
        stream=True,
        start_time=0,
        playback_speed=1.0
    ):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))

        if 'entries' in data:
            data = data['entries'][0]

        filename = data['url'] if stream else ytdl.prepare_filename(data)
        
        ffmpeg_options = FFMPEG_OPTIONS.copy()
        if start_time > 0:
            ffmpeg_options['before_options'] = f'-ss {start_time} ' + ffmpeg_options.get('before_options', '')
        if abs(playback_speed - 1.0) > PLAYBACK_SPEED_TOLERANCE:
            speed_value = f'{playback_speed:.3f}'.rstrip('0').rstrip('.')
            options = ffmpeg_options.get('options', '').strip()
            ffmpeg_options['options'] = f"{options} -af atempo={speed_value}".strip()

        return cls(
            discord.FFmpegPCMAudio(filename, **ffmpeg_options),
            data=data,
            start_time=start_time,
            playback_speed=playback_speed
        )

    @classmethod
    def extract_start_time(cls, url: str) -> int:
        """Extract start time from YouTube URL t parameter (in seconds)"""
        try:
            parsed_url = urlparse(url)
            query_params = parse_qs(parsed_url.query)
            
            if 't' in query_params:
                t_value = query_params['t'][0]
                # if t_value.endswith('s'):
                #     t_value = t_value[:-1]
                return int(t_value)
        except:
            pass
        return 0


class MusicQueue:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue = []
        self.current = None
        self.playback_start_time = None
        self.playback_speed = 1.0

    def add(self, item):
        self.queue.append(item)
        self.save_state()

    def next(self) -> Optional[dict]:
        if self.queue:
            self.current = self.queue.pop(0)
            return self.current
        self.current = None
        return None

    def clear(self, save_state=True):
        self.queue.clear()
        self.current = None
        self.playback_start_time = None
        self.playback_speed = 1.0
        if save_state:
            self.save_state()

    def is_empty(self) -> bool:
        return len(self.queue) == 0
    
    def get_current_position(self) -> int:
        """Get current playback position in seconds"""
        if self.current and self.playback_start_time:
            player = self.current.get('player')
            if not player:
                return 0
            elapsed = time.time() - self.playback_start_time
            progress = player.start_time + (elapsed * player.playback_speed)
            return int(progress)
        return 0
    
    def start_playback(self):
        """Mark the start of playback for position tracking"""
        if self.current:
            player = self.current.get('player')
            if player:
                self.playback_speed = player.playback_speed
        self.playback_start_time = time.time()
        self.save_state()
    
    def to_dict(self) -> dict:
        """Serialize queue state to dictionary"""
        queue_data = []
        for item in self.queue:
            player = item.get('player')
            if not player:
                continue
            queue_data.append({
                'title': player.title,
                'original_query': item.get('original_query', player.title),
                'duration': player.duration,
                'start_time': player.start_time,
                'playback_speed': player.playback_speed
            })
        
        current_data = None
        if self.current:
            player = self.current.get('player')
            if player:
                current_data = {
                    'title': player.title,
                    'original_query': self.current.get('original_query', player.title),
                    'duration': player.duration,
                    'position': self.get_current_position(),
                    'playback_speed': player.playback_speed
                }
        
        current_volume = None
        if self.current:
            ctx_reference = self.current.get('ctx')
            if ctx_reference and ctx_reference.voice_client and ctx_reference.voice_client.source:
                current_volume = ctx_reference.voice_client.source.volume
            else:
                interaction_ref = self.current.get('interaction')
                if (
                    interaction_ref
                    and interaction_ref.guild
                    and interaction_ref.guild.voice_client
                    and interaction_ref.guild.voice_client.source
                ):
                    current_volume = interaction_ref.guild.voice_client.source.volume
        
        return {
            'guild_id': self.guild_id,
            'queue': queue_data,
            'current': current_data,
            'current_volume': current_volume,
            'playback_speed': self.playback_speed,
            'timestamp': time.time()
        }
    
    def save_state(self):
        """Save queue state to JSON file"""
        try:
            state_file = f'queue_state_{self.guild_id}.json'
            with open(state_file, 'w') as f:
                json.dump(self.to_dict(), f, indent=2)
            logger.debug(f'Saved queue state for guild {self.guild_id}')
        except Exception as e:
            logger.error(traceback.format_exc())
            logger.error(f'Failed to save queue state for guild {self.guild_id}: {e}')
    
    @classmethod
    def load_state(cls, guild_id: int) -> Optional[dict]:
        """Load queue state from JSON file"""
        try:
            state_file = f'queue_state_{guild_id}.json'
            if os.path.exists(state_file):
                with open(state_file, 'r') as f:
                    data = json.load(f)
                logger.info(f'Loaded queue state for guild {guild_id}')
                return data
        except Exception as e:
            logger.error(f'Failed to load queue state for guild {guild_id}: {e}')
        return None


music_queues = {}
search_results = {}


def get_queue(guild_id: int) -> MusicQueue:
    if guild_id not in music_queues:
        music_queues[guild_id] = MusicQueue(guild_id)
    return music_queues[guild_id]


async def search_youtube(query: str, max_results: int = 10) -> list:
    """
    Search YouTube and return a list of results.
    Returns list of dicts with 'title', 'url', 'duration', 'channel' keys.
    """
    loop = asyncio.get_event_loop()
    try:
        search_opts = YTDL_OPTIONS.copy()
        search_opts['extract_flat'] = True
        search_opts['quiet'] = True
        
        def search_sync():
            with yt_dlp.YoutubeDL(search_opts) as ydl:
                return ydl.extract_info(f'ytsearch{max_results}:{query}', download=False)
        
        data = await loop.run_in_executor(None, search_sync)
        
        if not data or 'entries' not in data:
            return []
        
        results = []
        for entry in data['entries']:
            if entry:
                results.append({
                    'title': entry.get('title', 'Unknown'),
                    'url': entry.get('url', ''),
                    'duration': entry.get('duration', 0),
                    'channel': entry.get('channel', entry.get('uploader', 'Unknown')),
                    'id': entry.get('id', '')
                })
        
        return results
    except Exception as e:
        logger.error(f'YouTube search error: {e}')
        logger.error(traceback.format_exc())
        return []


async def periodic_state_saver():
    """Background task to periodically save queue states"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            for guild_id, queue in music_queues.items():
                if queue.current or not queue.is_empty():
                    queue.save_state()
            await asyncio.sleep(30)
        except Exception as e:
            logger.error(f'Error in periodic state saver: {e}')
            await asyncio.sleep(30)


@bot.event
async def on_ready():
    logger.info(f'{bot.user} has connected to Discord!')
    logger.info(f'Bot is ready to play music in {len(bot.guilds)} server(s)')
    try:
        synced = await bot.tree.sync()
        logger.info(f'Synced {len(synced)} slash command(s)')
    except Exception as e:
        logger.error(f'Failed to sync commands: {e}')
    
    bot.loop.create_task(periodic_state_saver())


@bot.event
async def on_error(event, *args, **kwargs):
    logger.error(f'Error in {event}:')
    logger.error(traceback.format_exc())


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f'Missing required argument: {error.param.name}')
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f'Bad argument: {error}')
    else:
        logger.error(f'Command error in {ctx.command}: {error}')
        logger.error(traceback.format_exc())
        await ctx.send(f'An error occurred while executing the command.')


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    
    await bot.process_commands(message)
    
    if message.author.id in search_results:
        search_data = search_results[message.author.id]
        
        if message.channel.id != search_data['channel_id']:
            return
        
        if time.time() - search_data['timestamp'] > 60:
            del search_results[message.author.id]
            return
        
        try:
            selection = int(message.content.strip())
            if 1 <= selection <= len(search_data['results']):
                selected = search_data['results'][selection - 1]
                
                del search_results[message.author.id]
                
                if not message.author.voice:
                    await message.channel.send('You need to be in a voice channel to play music.')
                    return
                
                voice_channel = message.author.voice.channel
                queue = get_queue(search_data['guild_id'])
                guild = bot.get_guild(search_data['guild_id'])
                voice_client = guild.voice_client if guild else None
                
                if voice_client is None:
                    voice_client = await voice_channel.connect(self_deaf=True)
                elif voice_client.channel != voice_channel:
                    await voice_client.move_to(voice_channel)
                
                async with message.channel.typing():
                    try:
                        video_url = f"https://www.youtube.com/watch?v={selected['id']}"
                        player = await YTDLSource.from_url(
                            video_url,
                            loop=bot.loop,
                            stream=True,
                            playback_speed=queue.playback_speed
                        )
                        
                        if 'ctx' in search_data:
                            ctx = search_data['ctx']
                            queue.add({'player': player, 'ctx': ctx, 'original_query': video_url})
                            
                            if not voice_client.is_playing():
                                await play_next(ctx)
                            else:
                                await message.channel.send(f'Added to queue: **{player.title}**')
                        else:
                            interaction = search_data['interaction']
                            queue.add({'player': player, 'interaction': interaction, 'original_query': video_url})
                            
                            if not voice_client.is_playing():
                                await play_next_slash(interaction)
                            else:
                                await message.channel.send(f'Added to queue: **{player.title}**')
                        
                        logger.info(f'User {message.author.id} selected search result {selection}')
                    except Exception as e:
                        await message.channel.send(f'An error occurred: {str(e)}')
                        logger.error(f'Error playing search result: {e}')
        except ValueError:
            pass


@bot.command(name='play', help='Plays audio from YouTube URL or search query')
async def play(ctx, *, query: str):
    if not ctx.author.voice:
        await ctx.send('You need to be in a voice channel to use this command.')
        return

    voice_channel = ctx.author.voice.channel
    queue = get_queue(ctx.guild.id)

    if ctx.voice_client is None:
        await voice_channel.connect(self_deaf=True)
    elif ctx.voice_client.channel != voice_channel:
        await ctx.voice_client.move_to(voice_channel)

    async with ctx.typing():
        try:
            start_time = YTDLSource.extract_start_time(query)
            player = await YTDLSource.from_url(
                query,
                loop=bot.loop,
                stream=True,
                start_time=start_time,
                playback_speed=queue.playback_speed
            )
            queue.add({'player': player, 'ctx': ctx, 'original_query': query})

            if not ctx.voice_client.is_playing():
                await play_next(ctx)
            else:
                await ctx.send(f'Added to queue: **{player.title}**')
        except Exception as e:
            await ctx.send(f'An error occurred: {str(e)}')


@bot.command(name='search', aliases=['s', 'find'], help='Search YouTube and select from results')
async def search(ctx, *, query: str):
    if not ctx.author.voice:
        await ctx.send('You need to be in a voice channel to use this command.')
        return
    
    async with ctx.typing():
        results = await search_youtube(query, max_results=10)
        
        if not results:
            await ctx.send('No results found for your search.')
            return
        
        embed = discord.Embed(
            title=f'Search Results for: {query}',
            description='Reply with a number (1-10) to select a song',
            color=discord.Color.blue()
        )
        
        for i, result in enumerate(results, 1):
            duration_str = format_duration(result['duration']) if result['duration'] else 'Live'
            embed.add_field(
                name=f"{i}. {result['title'][:80]}",
                value=f"Channel: {result['channel']} | Duration: {duration_str}",
                inline=False
            )
        
        embed.set_footer(text='This search will expire in 60 seconds')
        await ctx.send(embed=embed)
        
        search_results[ctx.author.id] = {
            'results': results,
            'channel_id': ctx.channel.id,
            'guild_id': ctx.guild.id,
            'timestamp': time.time(),
            'ctx': ctx
        }
        
        logger.info(f'User {ctx.author.id} searched for: {query}')


async def play_next(ctx):
    queue = get_queue(ctx.guild.id)
    
    if queue.is_empty():
        queue.current = None
        return

    item = queue.next()
    if item is None:
        return

    player = item['player']
    
    def after_playing(error):
        if error:
            logger.error(f'Player error in guild {ctx.guild.id}: {error}')
            logger.error(traceback.format_exc())
        try:
            asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
        except Exception as e:
            logger.error(f'Failed to queue next song: {e}')

    ctx.voice_client.play(player, after=after_playing)
    queue.start_playback()
    await ctx.send(f'Now playing: **{player.title}**')


@bot.command(name='pause', help='Pauses the current audio')
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send('Playback paused.')
    else:
        await ctx.send('Nothing is currently playing.')


@bot.command(name='resume', help='Resumes the paused audio')
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send('Playback resumed.')
    else:
        await ctx.send('Playback is not paused.')


@bot.command(name='skip', help='Skips the current song')
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send('Skipped to the next song.')
    else:
        await ctx.send('Nothing is currently playing.')


@bot.command(name='stop', help='Stops playback and clears the queue')
async def stop(ctx):
    queue = get_queue(ctx.guild.id)
    queue.clear()
    
    if ctx.voice_client:
        ctx.voice_client.stop()
        await ctx.send('Playback stopped and queue cleared.')
    else:
        await ctx.send('Nothing is currently playing.')


@bot.command(name='queue', help='Shows the current queue')
async def show_queue(ctx):
    queue = get_queue(ctx.guild.id)
    
    if queue.current is None and queue.is_empty():
        await ctx.send('The queue is empty.')
        return

    embed = discord.Embed(title='Music Queue', color=discord.Color.blue())
    
    if queue.current:
        current_title = queue.current['player'].title
        embed.add_field(name='Now Playing', value=f'🎵 {current_title}', inline=False)
    
    if not queue.is_empty():
        queue_text = '\n'.join([f'{i+1}. {item["player"].title}' for i, item in enumerate(queue.queue)])
        embed.add_field(name='Up Next', value=queue_text, inline=False)
    
    await ctx.send(embed=embed)


@bot.command(name='leave', help='Disconnects the bot from the voice channel')
async def leave(ctx):
    if ctx.voice_client:
        queue = get_queue(ctx.guild.id)
        queue.clear(save_state=False)
        await ctx.voice_client.disconnect()
        await ctx.send('Disconnected from voice channel.')
    else:
        await ctx.send('I am not in a voice channel.')


@bot.command(name='nowplaying', aliases=['np'], help='Shows the currently playing song')
async def nowplaying(ctx):
    queue = get_queue(ctx.guild.id)
    
    if queue.current is None:
        await ctx.send('Nothing is currently playing.')
        return

    player = queue.current['player']
    embed = discord.Embed(title='Now Playing', color=discord.Color.green())
    embed.add_field(name='Title', value=player.title, inline=False)
    
    if player.duration:
        minutes, seconds = divmod(player.duration, 60)
        embed.add_field(name='Duration', value=f'{int(minutes)}:{int(seconds):02d}', inline=True)
    
    await ctx.send(embed=embed)


@bot.command(name='volume', help='Changes the volume (0-200, where 100 is normal)')
async def volume(ctx, volume: int):
    if not ctx.voice_client:
        await ctx.send('I am not connected to a voice channel.')
        return

    if not 0 <= volume <= 200:
        await ctx.send('Volume must be between 0 and 200 (100 is normal, 200 is amplified).')
        return

    if ctx.voice_client.source:
        actual_volume = volume / 100
        ctx.voice_client.source.volume = actual_volume
        status = 'amplified' if volume > 100 else 'normal' if volume == 100 else 'reduced'
        await ctx.send(f'Volume set to {volume}% ({status})')
    else:
        await ctx.send('Nothing is currently playing.')


@bot.command(name='joke', help='Fetches a random joke')
async def joke(ctx):
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get('https://v2.jokeapi.dev/joke/Any?format=txt') as response:
                if response.status == 200:
                    joke_text = await response.text()
                    await ctx.send(joke_text.strip())
                else:
                    await ctx.send('Failed to fetch a joke. Please try again later.')
        except Exception as e:
            await ctx.send(f'An error occurred while fetching the joke: {str(e)}')


@bot.command(name='ia', help='Ask OpenAI a question')
async def ia(ctx, *, prompt: str):
    if not openai_client:
        await ctx.send('OpenAI API key not configured. Please set OPENAI_API_KEY in your .env file.')
        return
    
    async with ctx.typing():
        try:
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: openai_client.responses.create(
                    model="gpt-5-nano",
                    input=prompt
                )
            )
            
            output = response.output_text
            
            if len(output) > 2000:
                chunks = [output[i:i+2000] for i in range(0, len(output), 2000)]
                for chunk in chunks:
                    await ctx.send(chunk)
            else:
                await ctx.send(output)
            
            logger.info(f'OpenAI query from guild {ctx.guild.id}: {prompt[:50]}...')
            
        except Exception as e:
            logger.error(f'OpenAI API error: {e}')
            await ctx.send(f'An error occurred while calling OpenAI: {str(e)}')


@bot.command(name='restore', aliases=['resumesession'], help='Restore playback from saved session')
async def restore_session(ctx):
    if not ctx.author.voice:
        await ctx.send('You need to be in a voice channel to use this command.')
        return
    
    voice_channel = ctx.author.voice.channel
    queue = get_queue(ctx.guild.id)
    
    saved_state = MusicQueue.load_state(ctx.guild.id)
    if not saved_state:
        await ctx.send('No saved session found for this server.')
        return

    queue.playback_speed = saved_state.get('playback_speed', 1.0)
    logger.info(
        f'Restoring session for guild {ctx.guild.id} with speed {queue.playback_speed}x'
    )

    if not saved_state.get('current') and not saved_state.get('queue'):
        await ctx.send('Saved session is empty.')
        return
    
    if ctx.voice_client is None:
        await voice_channel.connect(self_deaf=True)
    elif ctx.voice_client.channel != voice_channel:
        await ctx.voice_client.move_to(voice_channel)
    
    async with ctx.typing():
        try:
            restored_count = 0
            
            if saved_state.get('current'):
                current = saved_state['current']
                logger.info(f'Resuming: {current["title"]} at position {current.get("position", 0)}s')
                
                position = current.get('position', 0)
                playback_speed = current.get('playback_speed', queue.playback_speed)
                player = await YTDLSource.from_url(
                    current['original_query'],
                    loop=bot.loop,
                    stream=True,
                    start_time=position,
                    playback_speed=playback_speed
                )
                queue.current = {'player': player, 'ctx': ctx, 'original_query': current['original_query']}
                
                def after_playing(error):
                    if error:
                        logger.error(f'Player error in guild {ctx.guild.id}: {error}')
                        logger.error(traceback.format_exc())
                    try:
                        asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
                    except Exception as e:
                        logger.error(f'Failed to queue next song: {e}')
                
                ctx.voice_client.play(player, after=after_playing)
                if saved_state.get('current_volume'):
                    logger.info(f'Restored volume to {saved_state["current_volume"]}')
                    ctx.voice_client.source.volume = saved_state['current_volume']
                queue.start_playback()
                restored_count += 1
                await ctx.send(f'Resumed: **{current["title"]}** at {format_duration(position)}')
            
            for item in saved_state.get('queue', []):
                try:
                    player = await YTDLSource.from_url(
                        item['original_query'],
                        loop=bot.loop,
                        stream=True,
                        start_time=item.get('start_time', 0),
                        playback_speed=item.get('playback_speed', queue.playback_speed)
                    )
                    queue.add({'player': player, 'ctx': ctx, 'original_query': item['original_query']})
                    restored_count += 1
                except Exception as e:
                    logger.error(f'Failed to restore song {item["title"]}: {e}')
            
            if restored_count > 1:
                await ctx.send(f'Restored {restored_count} song(s) from saved session.')
            
            logger.info(f'Guild {ctx.guild.id} resumed session with {restored_count} songs')
            
        except Exception as e:
            logger.error(f'Error resuming session: {e}')
            logger.error(traceback.format_exc())
            await ctx.send(f'An error occurred while resuming the session: {str(e)}')


@bot.command(name='status', aliases=['health'], help='Check bot status and connection health')
async def status(ctx):
    queue = get_queue(ctx.guild.id)
    
    embed = discord.Embed(title='Bot Status', color=discord.Color.blue())
    
    latency_ms = round(bot.latency * 1000)
    embed.add_field(name='Latency', value=f'{latency_ms}ms', inline=True)
    
    voice_status = 'Not connected'
    if ctx.voice_client:
        if ctx.voice_client.is_connected():
            if ctx.voice_client.is_playing():
                voice_status = '🎵 Playing'
            elif ctx.voice_client.is_paused():
                voice_status = '⏸️ Paused'
            else:
                voice_status = '✅ Connected (idle)'
        else:
            voice_status = '❌ Disconnected'
    
    embed.add_field(name='Voice Status', value=voice_status, inline=True)
    
    queue_info = f'{len(queue.queue)} song(s)' if not queue.is_empty() else 'Empty'
    embed.add_field(name='Queue', value=queue_info, inline=True)
    
    if queue.current:
        current_pos = queue.get_current_position()
        embed.add_field(
            name='Current Position',
            value=format_duration(current_pos),
            inline=True
        )
    
    embed.add_field(name='Servers', value=len(bot.guilds), inline=True)
    embed.add_field(name='Bot Version', value='1.0.0', inline=True)
    
    await ctx.send(embed=embed)


@bot.command(name='seek', help='Seek to a specific time in the current song (format: seconds or MM:SS)')
async def seek(ctx, *, time: str):
    queue = get_queue(ctx.guild.id)
    
    if queue.current is None:
        await ctx.send('Nothing is currently playing.')
        return
    
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        await ctx.send('I am not in a voice channel.')
        return
    
    try:
        seek_seconds = parse_time_input(time)
        
        player_data = queue.current['player']
        if player_data.duration and seek_seconds > player_data.duration:
            await ctx.send(f'Seek time exceeds song duration ({format_duration(player_data.duration)}).')
            return
        
        async with ctx.typing():
            original_query = queue.current.get('original_query', player_data.title)
            current_source = ctx.voice_client.source
            current_volume = getattr(current_source, 'volume', None) if current_source else None
            ctx.voice_client.stop()
            
            new_player = await YTDLSource.from_url(
                original_query,
                loop=bot.loop,
                stream=True,
                start_time=seek_seconds,
                playback_speed=player_data.playback_speed
            )
            
            metadata = dict(queue.current)
            metadata['player'] = new_player
            metadata['ctx'] = ctx
            metadata['original_query'] = original_query
            queue.current = metadata
            
            def after_playing(error):
                if error:
                    logger.error(f'Player error in guild {ctx.guild.id}: {error}')
                asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
            
            ctx.voice_client.play(new_player, after=after_playing)
            if current_volume is not None:
                new_player.volume = current_volume
            queue.start_playback()
            logger.info(f'Guild {ctx.guild.id} seeked to {seek_seconds}s')
            await ctx.send(f'Seeked to {format_duration(seek_seconds)} in **{new_player.title}**')
            
    except ValueError as e:
        await ctx.send(f'Invalid time format. Use seconds (e.g., 90) or MM:SS format (e.g., 1:30).')
    except Exception as e:
        await ctx.send(f'An error occurred while seeking: {str(e)}')


@bot.command(name='forward', aliases=['fwd', 'jump'], help='Skip forward or backward by seconds (use negative for backward)')
async def forward(ctx, seconds: int):
    queue = get_queue(ctx.guild.id)
    
    if queue.current is None:
        await ctx.send('Nothing is currently playing.')
        return
    
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        await ctx.send('I am not in a voice channel.')
        return
    
    try:
        current_position = queue.get_current_position()
        new_position = current_position + seconds
        
        if new_position < 0:
            new_position = 0
            await ctx.send('Cannot skip before the start. Starting from beginning.')
        
        player_data = queue.current['player']
        if player_data.duration and new_position > player_data.duration:
            await ctx.send(f'Cannot skip beyond song duration. Use skip to go to next song.')
            return
        
        async with ctx.typing():
            original_query = queue.current.get('original_query', player_data.title)
            current_source = ctx.voice_client.source
            current_volume = getattr(current_source, 'volume', None) if current_source else None
            ctx.voice_client.stop()
            
            new_player = await YTDLSource.from_url(
                original_query,
                loop=bot.loop,
                stream=True,
                start_time=new_position,
                playback_speed=player_data.playback_speed
            )
            
            metadata = dict(queue.current)
            metadata['player'] = new_player
            metadata['ctx'] = ctx
            metadata['original_query'] = original_query
            queue.current = metadata
            
            def after_playing(error):
                if error:
                    logger.error(f'Player error in guild {ctx.guild.id}: {error}')
                asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
            
            ctx.voice_client.play(new_player, after=after_playing)
            if current_volume is not None:
                new_player.volume = current_volume
            queue.start_playback()
            
            direction = 'forward' if seconds > 0 else 'backward'
            logger.info(
                f'Guild {ctx.guild.id} skipped {direction} {seconds}s to position {new_position}s'
            )
            await ctx.send(
                f'Skipped {direction} {abs(seconds)}s to {format_duration(new_position)} '
                f'in **{new_player.title}**'
            )
    except Exception as e:
        await ctx.send(f'An error occurred while skipping: {str(e)}')


@bot.command(name='speed', aliases=['tempo'], help='Change playback speed (0.5x-2.0x)')
async def change_speed(ctx, speed: float):
    queue = get_queue(ctx.guild.id)
    if queue.current is None:
        await ctx.send('Nothing is currently playing.')
        return

    voice_client = ctx.voice_client
    if not voice_client or not voice_client.is_connected():
        await ctx.send('I am not in a voice channel.')
        return

    if not MIN_PLAYBACK_SPEED <= speed <= MAX_PLAYBACK_SPEED:
        await ctx.send(
            f'Playback speed must be between {MIN_PLAYBACK_SPEED}x and {MAX_PLAYBACK_SPEED}x.'
        )
        return

    current_item = queue.current
    current_player = current_item.get('player') if current_item else None
    if not current_player:
        await ctx.send('Playback data is not available.')
        return

    if abs(current_player.playback_speed - speed) <= PLAYBACK_SPEED_TOLERANCE:
        await ctx.send(f'Playback speed is already {format_speed(speed)}x.')
        return

    async with ctx.typing():
        try:
            original_query = current_item.get('original_query', current_player.title)
            current_position = queue.get_current_position()
            if current_player.duration and current_position >= current_player.duration:
                current_position = max(current_player.duration - 1, 0)

            current_source = voice_client.source
            current_volume = getattr(current_source, 'volume', None) if current_source else None
            voice_client.stop()

            new_player = await YTDLSource.from_url(
                original_query,
                loop=bot.loop,
                stream=True,
                start_time=current_position,
                playback_speed=speed
            )

            metadata = dict(current_item)
            metadata['player'] = new_player
            metadata['ctx'] = ctx
            metadata['original_query'] = original_query
            queue.current = metadata

            def after_playing(error):
                if error:
                    logger.error(f'Player error in guild {ctx.guild.id}: {error}')
                    logger.error(traceback.format_exc())
                try:
                    asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
                except Exception as exc:
                    logger.error(f'Failed to queue next song: {exc}')

            voice_client.play(new_player, after=after_playing)
            if current_volume is not None:
                new_player.volume = current_volume
            queue.start_playback()

            await ctx.send(
                f'Playback speed set to {format_speed(speed)}x at '
                f'{format_duration(current_position)} in **{new_player.title}**'
            )
        except Exception as e:
            logger.error(f'Error changing playback speed in guild {ctx.guild.id}: {e}')
            logger.error(traceback.format_exc())
            await ctx.send(f'An error occurred while changing playback speed: {str(e)}')


def parse_time_input(time_str: str) -> int:
    """Parse time input from various formats to seconds"""
    time_str = time_str.strip()
    
    if ':' in time_str:
        parts = time_str.split(':')
        if len(parts) == 2:
            minutes, seconds = int(parts[0]), int(parts[1])
            return minutes * 60 + seconds
        elif len(parts) == 3:
            hours, minutes, seconds = int(parts[0]), int(parts[1]), int(parts[2])
            return hours * 3600 + minutes * 60 + seconds
    else:
        return int(time_str)
    
    raise ValueError('Invalid time format')


def format_duration(seconds: int) -> str:
    """Format seconds into MM:SS or HH:MM:SS"""
    if seconds < 3600:
        minutes, secs = divmod(seconds, 60)
        return f'{int(minutes)}:{int(secs):02d}'
    else:
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        return f'{int(hours)}:{int(minutes):02d}:{int(secs):02d}'


def format_speed(speed: float) -> str:
    value = f'{speed:.2f}'
    return value.rstrip('0').rstrip('.')


# Slash Commands
@bot.tree.command(name='play', description='Play audio from YouTube URL or search query')
@app_commands.describe(query='YouTube URL or search query')
async def slash_play(interaction: discord.Interaction, query: str):
    if not interaction.user.voice:
        await interaction.response.send_message('You need to be in a voice channel to use this command.', ephemeral=True)
        return

    voice_channel = interaction.user.voice.channel
    guild_id = interaction.guild.id
    queue = get_queue(guild_id)

    voice_client = interaction.guild.voice_client
    if voice_client is None:
        voice_client = await voice_channel.connect(self_deaf=True)
    elif voice_client.channel != voice_channel:
        await voice_client.move_to(voice_channel)

    await interaction.response.defer()
    
    try:
        start_time = YTDLSource.extract_start_time(query)
        player = await YTDLSource.from_url(
            query,
            loop=bot.loop,
            stream=True,
            start_time=start_time,
            playback_speed=queue.playback_speed
        )
        queue.add({'player': player, 'interaction': interaction, 'original_query': query})

        if not voice_client.is_playing():
            await play_next_slash(interaction)
        else:
            await interaction.followup.send(f'Added to queue: **{player.title}**')
    except Exception as e:
        await interaction.followup.send(f'An error occurred: {str(e)}')


async def play_next_slash(interaction: discord.Interaction):
    queue = get_queue(interaction.guild.id)
    voice_client = interaction.guild.voice_client
    
    if queue.is_empty():
        queue.current = None
        return

    item = queue.next()
    if item is None:
        return

    player = item['player']
    
    def after_playing(error):
        if error:
            logger.error(f'Player error in guild {interaction.guild.id}: {error}')
            logger.error(traceback.format_exc())
        try:
            asyncio.run_coroutine_threadsafe(play_next_slash(interaction), bot.loop)
        except Exception as e:
            logger.error(f'Failed to queue next song: {e}')

    voice_client.play(player, after=after_playing)
    queue.start_playback()
    
    try:
        if 'interaction' in item:
            await item['interaction'].followup.send(f'Now playing: **{player.title}**')
        else:
            channel = interaction.channel
            await channel.send(f'Now playing: **{player.title}**')
    except Exception as e:
        logger.error(f'Failed to send now playing message: {e}')


@bot.tree.command(name='pause', description='Pause the current audio')
async def slash_pause(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.pause()
        await interaction.response.send_message('Playback paused.')
    else:
        await interaction.response.send_message('Nothing is currently playing.', ephemeral=True)


@bot.tree.command(name='resume', description='Resume the paused audio')
async def slash_resume(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_paused():
        voice_client.resume()
        await interaction.response.send_message('Playback resumed.')
    else:
        await interaction.response.send_message('Playback is not paused.', ephemeral=True)


@bot.tree.command(name='skip', description='Skip the current song')
async def slash_skip(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.stop()
        await interaction.response.send_message('Skipped to the next song.')
    else:
        await interaction.response.send_message('Nothing is currently playing.', ephemeral=True)


@bot.tree.command(name='stop', description='Stop playback and clear the queue')
async def slash_stop(interaction: discord.Interaction):
    queue = get_queue(interaction.guild.id)
    queue.clear()
    
    voice_client = interaction.guild.voice_client
    if voice_client:
        voice_client.stop()
        await interaction.response.send_message('Playback stopped and queue cleared.')
    else:
        await interaction.response.send_message('Nothing is currently playing.', ephemeral=True)


@bot.tree.command(name='queue', description='Show the current queue')
async def slash_queue(interaction: discord.Interaction):
    queue = get_queue(interaction.guild.id)
    
    if queue.current is None and queue.is_empty():
        await interaction.response.send_message('The queue is empty.', ephemeral=True)
        return

    embed = discord.Embed(title='Music Queue', color=discord.Color.blue())
    
    if queue.current:
        current_title = queue.current['player'].title
        embed.add_field(name='Now Playing', value=f'🎵 {current_title}', inline=False)
    
    if not queue.is_empty():
        queue_text = '\n'.join([f'{i+1}. {item["player"].title}' for i, item in enumerate(queue.queue)])
        embed.add_field(name='Up Next', value=queue_text, inline=False)
    
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name='leave', description='Disconnect the bot from the voice channel')
async def slash_leave(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client:
        queue = get_queue(interaction.guild.id)
        queue.clear()
        await voice_client.disconnect()
        await interaction.response.send_message('Disconnected from voice channel.')
    else:
        await interaction.response.send_message('I am not in a voice channel.', ephemeral=True)


@bot.tree.command(name='nowplaying', description='Show the currently playing song')
async def slash_nowplaying(interaction: discord.Interaction):
    queue = get_queue(interaction.guild.id)
    
    if queue.current is None:
        await interaction.response.send_message('Nothing is currently playing.', ephemeral=True)
        return

    player = queue.current['player']
    embed = discord.Embed(title='Now Playing', color=discord.Color.green())
    embed.add_field(name='Title', value=player.title, inline=False)
    
    if player.duration:
        minutes, seconds = divmod(player.duration, 60)
        embed.add_field(name='Duration', value=f'{int(minutes)}:{int(seconds):02d}', inline=True)
    
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name='volume', description='Change the volume (0-200, where 100 is normal)')
@app_commands.describe(volume='Volume level from 0 to 200 (100=normal, 200=amplified)')
async def slash_volume(interaction: discord.Interaction, volume: int):
    voice_client = interaction.guild.voice_client
    if not voice_client:
        await interaction.response.send_message('I am not connected to a voice channel.', ephemeral=True)
        return

    if not 0 <= volume <= 200:
        await interaction.response.send_message('Volume must be between 0 and 200 (100 is normal, 200 is amplified).', ephemeral=True)
        return

    if voice_client.source:
        actual_volume = volume / 100
        voice_client.source.volume = actual_volume
        status = 'amplified' if volume > 100 else 'normal' if volume == 100 else 'reduced'
        await interaction.response.send_message(f'Volume set to {volume}% ({status})')
    else:
        await interaction.response.send_message('Nothing is currently playing.', ephemeral=True)


@bot.tree.command(name='joke', description='Get a random joke')
async def slash_joke(interaction: discord.Interaction):
    await interaction.response.defer()
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get('https://v2.jokeapi.dev/joke/Any?format=txt') as response:
                if response.status == 200:
                    joke_text = await response.text()
                    await interaction.followup.send(joke_text.strip())
                else:
                    await interaction.followup.send('Failed to fetch a joke. Please try again later.')
        except Exception as e:
            await interaction.followup.send(f'An error occurred while fetching the joke: {str(e)}')


@bot.tree.command(name='seek', description='Seek to a specific time in the current song')
@app_commands.describe(time='Time in seconds or MM:SS format (e.g., 90 or 1:30)')
async def slash_seek(interaction: discord.Interaction, time: str):
    queue = get_queue(interaction.guild.id)
    
    if queue.current is None:
        await interaction.response.send_message('Nothing is currently playing.', ephemeral=True)
        return
    
    voice_client = interaction.guild.voice_client
    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message('I am not in a voice channel.', ephemeral=True)
        return
    
    try:
        seek_seconds = parse_time_input(time)
        
        player_data = queue.current['player']
        if player_data.duration and seek_seconds > player_data.duration:
            await interaction.response.send_message(
                f'Seek time exceeds song duration ({format_duration(player_data.duration)}).',
                ephemeral=True
            )
            return
        
        await interaction.response.defer()
        
        original_query = queue.current.get('original_query', player_data.title)
        voice_client.stop()
        
        new_player = await YTDLSource.from_url(
            original_query,
            loop=bot.loop,
            stream=True,
            start_time=seek_seconds,
            playback_speed=player_data.playback_speed
        )
        
        metadata = dict(queue.current)
        metadata['player'] = new_player
        metadata['interaction'] = interaction
        metadata['original_query'] = original_query
        queue.current = metadata
        
        def after_playing(error):
            if error:
                logger.error(f'Player error in guild {interaction.guild.id}: {error}')
            asyncio.run_coroutine_threadsafe(play_next_slash(interaction), bot.loop)
        
        current_source = voice_client.source
        current_volume = getattr(current_source, 'volume', None) if current_source else None
        voice_client.play(new_player, after=after_playing)
        if current_volume is not None:
            new_player.volume = current_volume
        queue.start_playback()
        await interaction.followup.send(
            f'Seeked to {format_duration(seek_seconds)} in **{new_player.title}**'
        )
        
    except ValueError:
        await interaction.response.send_message(
            'Invalid time format. Use seconds (e.g., 90) or MM:SS format (e.g., 1:30).',
            ephemeral=True
        )
    except Exception as e:
        if interaction.response.is_done():
            await interaction.followup.send(f'An error occurred while seeking: {str(e)}')
        else:
            await interaction.response.send_message(f'An error occurred while seeking: {str(e)}', ephemeral=True)


@bot.tree.command(name='forward', description='Skip forward or backward by seconds')
@app_commands.describe(seconds='Number of seconds to skip (use negative for backward)')
async def slash_forward(interaction: discord.Interaction, seconds: int):
    queue = get_queue(interaction.guild.id)
    
    if queue.current is None:
        await interaction.response.send_message('Nothing is currently playing.', ephemeral=True)
        return
    
    voice_client = interaction.guild.voice_client
    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message('I am not in a voice channel.', ephemeral=True)
        return
    
    try:
        current_position = queue.get_current_position()
        new_position = current_position + seconds
        
        if new_position < 0:
            new_position = 0
        
        player_data = queue.current['player']
        if player_data.duration and new_position > player_data.duration:
            await interaction.response.send_message(
                'Cannot skip beyond song duration. Use skip to go to next song.',
                ephemeral=True
            )
            return
        
        await interaction.response.defer()
        
        original_query = queue.current.get('original_query', player_data.title)
        voice_client.stop()
        
        new_player = await YTDLSource.from_url(
            original_query,
            loop=bot.loop,
            stream=True,
            start_time=new_position,
            playback_speed=player_data.playback_speed
        )
        
        metadata = dict(queue.current)
        metadata['player'] = new_player
        metadata['interaction'] = interaction
        metadata['original_query'] = original_query
        queue.current = metadata
        
        def after_playing(error):
            if error:
                logger.error(f'Player error in guild {interaction.guild.id}: {error}')
            asyncio.run_coroutine_threadsafe(play_next_slash(interaction), bot.loop)
        
        current_source = voice_client.source
        current_volume = getattr(current_source, 'volume', None) if current_source else None
        voice_client.play(new_player, after=after_playing)
        if current_volume is not None:
            new_player.volume = current_volume
        queue.start_playback()
        
        direction = 'forward' if seconds > 0 else 'backward'
        await interaction.followup.send(
            f'Skipped {direction} {abs(seconds)}s to {format_duration(new_position)} '
            f'in **{new_player.title}**'
        )
        
    except Exception as e:
        if interaction.response.is_done():
            await interaction.followup.send(f'An error occurred while skipping: {str(e)}')
        else:
            await interaction.response.send_message(f'An error occurred while skipping: {str(e)}', ephemeral=True)


@bot.tree.command(name='speed', description='Change playback speed (0.5x-2.0x)')
@app_commands.describe(speed='Playback speed multiplier between 0.5x and 2.0x')
async def slash_speed(interaction: discord.Interaction, speed: float):
    queue = get_queue(interaction.guild.id)
    if queue.current is None:
        await interaction.response.send_message('Nothing is currently playing.', ephemeral=True)
        return

    voice_client = interaction.guild.voice_client
    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message('I am not in a voice channel.', ephemeral=True)
        return

    if not MIN_PLAYBACK_SPEED <= speed <= MAX_PLAYBACK_SPEED:
        await interaction.response.send_message(
            f'Playback speed must be between {MIN_PLAYBACK_SPEED}x and {MAX_PLAYBACK_SPEED}x.',
            ephemeral=True
        )
        return

    current_item = queue.current
    current_player = current_item.get('player') if current_item else None
    if not current_player:
        await interaction.response.send_message('Playback data is not available.', ephemeral=True)
        return

    if abs(current_player.playback_speed - speed) <= PLAYBACK_SPEED_TOLERANCE:
        await interaction.response.send_message(
            f'Playback speed is already {format_speed(speed)}x.',
            ephemeral=True
        )
        return

    await interaction.response.defer()
    try:
        original_query = current_item.get('original_query', current_player.title)
        current_position = queue.get_current_position()
        if current_player.duration and current_position >= current_player.duration:
            current_position = max(current_player.duration - 1, 0)

        current_source = voice_client.source
        current_volume = getattr(current_source, 'volume', None) if current_source else None
        voice_client.stop()

        new_player = await YTDLSource.from_url(
            original_query,
            loop=bot.loop,
            stream=True,
            start_time=current_position,
            playback_speed=speed
        )

        metadata = dict(current_item)
        metadata['player'] = new_player
        metadata['interaction'] = interaction
        metadata['original_query'] = original_query
        queue.current = metadata

        def after_playing(error):
            if error:
                logger.error(f'Player error in guild {interaction.guild.id}: {error}')
                logger.error(traceback.format_exc())
            try:
                asyncio.run_coroutine_threadsafe(play_next_slash(interaction), bot.loop)
            except Exception as exc:
                logger.error(f'Failed to queue next song: {exc}')

        voice_client.play(new_player, after=after_playing)
        if current_volume is not None:
            new_player.volume = current_volume
        queue.start_playback()

        await interaction.followup.send(
            f'Playback speed set to {format_speed(speed)}x at '
            f'{format_duration(current_position)} in **{new_player.title}**'
        )
    except Exception as e:
        logger.error(f'Error changing playback speed in guild {interaction.guild.id}: {e}')
        logger.error(traceback.format_exc())
        await interaction.followup.send(
            f'An error occurred while changing playback speed: {str(e)}'
        )


@bot.tree.command(name='status', description='Check bot status and connection health')
async def slash_status(interaction: discord.Interaction):
    queue = get_queue(interaction.guild.id)
    
    embed = discord.Embed(title='Bot Status', color=discord.Color.blue())
    
    latency_ms = round(bot.latency * 1000)
    embed.add_field(name='Latency', value=f'{latency_ms}ms', inline=True)
    
    voice_client = interaction.guild.voice_client
    voice_status = 'Not connected'
    if voice_client:
        if voice_client.is_connected():
            if voice_client.is_playing():
                voice_status = '🎵 Playing'
            elif voice_client.is_paused():
                voice_status = '⏸️ Paused'
            else:
                voice_status = '✅ Connected (idle)'
        else:
            voice_status = '❌ Disconnected'
    
    embed.add_field(name='Voice Status', value=voice_status, inline=True)
    
    queue_info = f'{len(queue.queue)} song(s)' if not queue.is_empty() else 'Empty'
    embed.add_field(name='Queue', value=queue_info, inline=True)
    
    if queue.current:
        current_pos = queue.get_current_position()
        embed.add_field(
            name='Current Position',
            value=format_duration(current_pos),
            inline=True
        )
    
    embed.add_field(name='Servers', value=len(bot.guilds), inline=True)
    embed.add_field(name='Bot Version', value='1.0.0', inline=True)
    
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name='ia', description='Ask OpenAI a question')
@app_commands.describe(prompt='Your question or prompt for OpenAI')
async def slash_ia(interaction: discord.Interaction, prompt: str):
    if not openai_client:
        await interaction.response.send_message('OpenAI API key not configured. Please set OPENAI_API_KEY in your .env file.', ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: openai_client.responses.create(
                model="gpt-5-nano",
                input=prompt
            )
        )
        
        output = response.output_text
        
        if len(output) > 2000:
            chunks = [output[i:i+2000] for i in range(0, len(output), 2000)]
            await interaction.followup.send(chunks[0])
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk)
        else:
            await interaction.followup.send(output)
        
        logger.info(f'OpenAI query from guild {interaction.guild.id}: {prompt[:50]}...')
        
    except Exception as e:
        logger.error(f'OpenAI API error: {e}')
        await interaction.followup.send(f'An error occurred while calling OpenAI: {str(e)}')


@bot.tree.command(name='restore', description='Restore playback from saved session')
async def slash_restore_session(interaction: discord.Interaction):
    if not interaction.user.voice:
        await interaction.response.send_message('You need to be in a voice channel to use this command.', ephemeral=True)
        return
    
    voice_channel = interaction.user.voice.channel
    queue = get_queue(interaction.guild.id)
    
    saved_state = MusicQueue.load_state(interaction.guild.id)
    if not saved_state:
        await interaction.response.send_message('No saved session found for this server.', ephemeral=True)
        return

    queue.playback_speed = saved_state.get('playback_speed', 1.0)
    logger.info(
        f'Restoring session for guild {interaction.guild.id} with speed {queue.playback_speed}x'
    )

    if not saved_state.get('current') and not saved_state.get('queue'):
        await interaction.response.send_message('Saved session is empty.', ephemeral=True)
        return
    
    voice_client = interaction.guild.voice_client
    if voice_client is None:
        voice_client = await voice_channel.connect(self_deaf=True)
    elif voice_client.channel != voice_channel:
        await voice_client.move_to(voice_channel)
    
    await interaction.response.defer()
    
    try:
        restored_count = 0
        
        if saved_state.get('current'):
            current = saved_state['current']
            logger.info(f'Resuming: {current["title"]} at position {current.get("position", 0)}s')
            
            position = current.get('position', 0)
            playback_speed = current.get('playback_speed', queue.playback_speed)
            player = await YTDLSource.from_url(
                current['original_query'],
                loop=bot.loop,
                stream=True,
                start_time=position,
                playback_speed=playback_speed
            )
            queue.current = {'player': player, 'interaction': interaction, 'original_query': current['original_query']}
            
            def after_playing(error):
                if error:
                    logger.error(f'Player error in guild {interaction.guild.id}: {error}')
                    logger.error(traceback.format_exc())
                try:
                    asyncio.run_coroutine_threadsafe(play_next_slash(interaction), bot.loop)
                except Exception as e:
                    logger.error(f'Failed to queue next song: {e}')
            
            voice_client.play(player, after=after_playing)
            if saved_state.get('current_volume'):
                logger.info(f'Restored volume to {saved_state["current_volume"]}')
                voice_client.source.volume = saved_state['current_volume']
            queue.start_playback()
            restored_count += 1
            await interaction.followup.send(f'Resumed: **{current["title"]}** at {format_duration(position)}')
        
        for item in saved_state.get('queue', []):
            try:
                player = await YTDLSource.from_url(
                    item['original_query'],
                    loop=bot.loop,
                    stream=True,
                    start_time=item.get('start_time', 0),
                    playback_speed=item.get('playback_speed', queue.playback_speed)
                )
                queue.add({'player': player, 'interaction': interaction, 'original_query': item['original_query']})
                restored_count += 1
            except Exception as e:
                logger.error(f'Failed to restore song {item["title"]}: {e}')
        
        if restored_count > 1:
            await interaction.followup.send(f'Restored {restored_count} song(s) from saved session.')
        
        logger.info(f'Guild {interaction.guild.id} resumed session with {restored_count} songs')
        
    except Exception as e:
        logger.error(f'Error resuming session: {e}')
        logger.error(traceback.format_exc())
        if interaction.response.is_done():
            await interaction.followup.send(f'An error occurred while resuming the session: {str(e)}')
        else:
            await interaction.response.send_message(f'An error occurred while resuming the session: {str(e)}', ephemeral=True)


@bot.tree.command(name='search', description='Search YouTube and select from results')
@app_commands.describe(query='Search query for YouTube')
async def slash_search(interaction: discord.Interaction, query: str):
    if not interaction.user.voice:
        await interaction.response.send_message('You need to be in a voice channel to use this command.', ephemeral=True)
        return
    
    await interaction.response.defer()
    
    results = await search_youtube(query, max_results=10)
    
    if not results:
        await interaction.followup.send('No results found for your search.')
        return
    
    embed = discord.Embed(
        title=f'Search Results for: {query}',
        description='Reply with a number (1-10) to select a song',
        color=discord.Color.blue()
    )
    
    for i, result in enumerate(results, 1):
        duration_str = format_duration(result['duration']) if result['duration'] else 'Live'
        embed.add_field(
            name=f"{i}. {result['title'][:80]}",
            value=f"Channel: {result['channel']} | Duration: {duration_str}",
            inline=False
        )
    
    embed.set_footer(text='This search will expire in 60 seconds')
    await interaction.followup.send(embed=embed)
    
    search_results[interaction.user.id] = {
        'results': results,
        'channel_id': interaction.channel.id,
        'guild_id': interaction.guild.id,
        'timestamp': time.time(),
        'interaction': interaction
    }
    
    logger.info(f'User {interaction.user.id} searched for: {query}')


def main():
    if not DISCORD_TOKEN:
        print('Error: DISCORD_TOKEN not found in environment variables.')
        print('Please create a .env file with your Discord bot token.')
        return

    bot.run(DISCORD_TOKEN)


if __name__ == '__main__':
    main()
