# Musicólogo - Discord Music Bot

A Discord bot that plays and streams audio from YouTube with queue management.

## Features

- Stream audio from YouTube URLs or search queries
- Interactive YouTube search with numbered result selection
- Queue management system
- Playback controls (play, pause, resume, skip, stop, seek, forward)
- Volume control
- Display current song and queue
- Session persistence and restoration
- Multi-server support
- OpenAI integration (optional)

## Requirements

- Python 3.8 or higher
- FFmpeg installed and available in system PATH
- Discord Bot Token

## Installation

### 1. Install FFmpeg

**Windows:**
- Download FFmpeg from [ffmpeg.org](https://ffmpeg.org/download.html)
- Extract the files and add the `bin` folder to your system PATH
- Verify installation: `ffmpeg -version`

**Linux:**
```bash
sudo apt update
sudo apt install ffmpeg
```

**macOS:**
```bash
brew install ffmpeg
```

### 2. Clone and Setup

```bash
python -m venv .venv
```

**Activate virtual environment:**
- Windows: `.venv\Scripts\activate`
- Linux/macOS: `source .venv/bin/activate`

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Bot

1. Create a Discord application and bot at [Discord Developer Portal](https://discord.com/developers/applications)
2. Enable the following Privileged Gateway Intents:
   - MESSAGE CONTENT INTENT
   - SERVER MEMBERS INTENT
3. Copy your bot token
4. Create a `.env` file in the project root:

```bash
cp .env.example .env
```

5. Edit `.env` and add your configuration:

```
DISCORD_TOKEN=your_actual_bot_token_here
COMMAND_PREFIX=!
OPENAI_API_KEY=your_openai_api_key_here
```

**Note**: OpenAI API key is optional. If not provided, the `!ia` command will not work, but all other features will function normally.

To get an OpenAI API key:
- Go to [OpenAI Platform](https://platform.openai.com/)
- Sign up or log in
- Navigate to API Keys section
- Create a new API key

### 5. Invite Bot to Server

Generate an invite URL with these permissions:
- Scopes: `bot`, `applications.commands`
- Bot Permissions:
  - Read Messages/View Channels
  - Send Messages
  - Connect
  - Speak
  - Use Voice Activity

Or use this URL template (replace YOUR_CLIENT_ID):
```
https://discord.com/api/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=36700160&scope=bot%20applications.commands
```

**Note:** If you already invited the bot before, you need to reinvite it with the updated URL to enable slash commands.

## Usage

### Starting the Bot

#### Option 1: Run Directly

```bash
python bot.py
```

#### Option 2: Run with Docker

1. Build the Docker image:
```bash
docker build -t musicologo .
```

2. Run the container:
```bash
docker run -d --name musicologo-bot musicologo
```

**Note:** Make sure your `.env` file exists in the project directory before building the image.

To view logs:
```bash
docker logs -f musicologo-bot
```

To stop the bot:
```bash
docker stop musicologo-bot
```

### Commands

The bot supports both **prefix commands** (e.g., `!play`) and **slash commands** (e.g., `/play`).

#### Using Slash Commands (Recommended)

Type `/` in Discord to see all available commands with descriptions and autocomplete:

- **/play** - Play audio from YouTube URL or search query
  - Supports YouTube URLs with timestamps (e.g., `?t=90` will start at 1:30)
- **/search** - Search YouTube and select from top 10 results
  - Example: `/search mix pop`
  - Reply with a number (1-10) to select and play a song
- **/pause** - Pause current playback
- **/resume** - Resume paused playback
- **/skip** - Skip to the next song in queue
- **/stop** - Stop playback and clear the queue
- **/seek** - Jump to a specific time in the current song
  - Example: `/seek 90` (90 seconds) or `/seek 1:30` (1 minute 30 seconds)
- **/forward** - Skip forward or backward by seconds from current position
  - Example: `/forward 30` (skip 30 seconds ahead)
  - Example: `/forward -15` (skip 15 seconds back)
- **/queue** - Display the current queue
- **/nowplaying** - Show currently playing song
- **/volume** - Set playback volume (0-200, where 100 is normal)
- **/leave** - Disconnect bot from voice channel
- **/joke** - Get a random joke
- **/ia** - Ask OpenAI a question
- **/status** - Check bot health and connection status
- **/restore** - Restore playback from saved session

#### Using Prefix Commands

All prefix commands use the prefix defined in `.env` (default: `!`)

- **!play <URL or search query>** - Play audio from YouTube
  - Example: `!play https://www.youtube.com/watch?v=dQw4w9WgXcQ`
  - Example: `!play https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=90` (starts at 1:30)
  - Example: `!play never gonna give you up`
- **!search <query>** - Search YouTube and select from top 10 results
  - Aliases: `!s`, `!find`
  - Example: `!search mix pop`
  - Reply with a number (1-10) to select and play a song
  - Search expires after 60 seconds
- **!pause** - Pause current playback
- **!resume** - Resume paused playback
- **!skip** - Skip to the next song in queue
- **!stop** - Stop playback and clear the queue
- **!seek <time>** - Jump to a specific time in the current song
  - Example: `!seek 90` (jump to 90 seconds)
  - Example: `!seek 1:30` (jump to 1 minute 30 seconds)
  - Example: `!seek 1:30:45` (jump to 1 hour 30 minutes 45 seconds)
- **!forward <seconds>** - Skip forward or backward by seconds from current position
  - Aliases: `!fwd`, `!jump`
  - Example: `!forward 30` (skip 30 seconds ahead)
  - Example: `!forward -15` (skip 15 seconds back)
- **!queue** - Display the current queue
- **!nowplaying** or **!np** - Show currently playing song
- **!volume <0-200>** - Set playback volume (100 is normal, 200 is amplified)
- **!leave** - Disconnect bot from voice channel
- **!joke** - Get a random joke
- **!ia <prompt>** - Ask OpenAI a question
  - Example: `!ia Write a haiku about music`
- **!status** - Check bot health and connection status
  - Aliases: `!health`
- **!restore** - Restore playback from saved session
  - Aliases: `!resumesession`

## Project Structure

```
musicologo/
├── bot.py              # Main bot implementation
├── requirements.txt    # Python dependencies
├── .env               # Configuration (create from .env.example)
├── .env.example       # Example configuration
├── .gitignore         # Git ignore rules
├── .dockerignore      # Docker ignore rules
├── Dockerfile         # Docker container configuration
├── AGENTS.md          # Project guidelines
└── README.md          # This file
```

## Technical Details

### Dependencies

- **discord.py** - Discord API wrapper
- **yt-dlp** - YouTube audio extraction
- **PyNaCl** - Voice support for Discord
- **python-dotenv** - Environment variable management
- **aiohttp** - Async HTTP client
- **openai** - OpenAI API integration (optional)

### Architecture

- **YTDLSource** - Handles YouTube audio extraction and streaming
- **MusicQueue** - Per-server queue management
- **Commands** - Discord command handlers for music control

## Session Persistence

### Automatic State Saving
The bot automatically saves your session state to disk:
- **What's saved**: Current song (with playback position), entire queue
- **When**: Automatically every 30 seconds while playing
- **Where**: `queue_state_<guild_id>.json` files (one per server)

### Restore Command
After a bot restart or disconnection, restore your session:

```bash
!restore  # or /restore
```

This will:
1. Load the saved session for your server
2. Resume the current song from where it left off
3. Restore all queued songs in order

**Use Cases**:
- Bot crashed or was restarted
- You stopped the bot temporarily
- Want to continue yesterday's playlist

**Note**: Session files are automatically created and updated. No manual action needed.

## Error Handling & Monitoring

### Logging
The bot automatically logs all events to both console and `bot.log` file:
- Info: Connection events, command usage, playback events
- Errors: Playback failures, network issues, command errors

### Health Monitoring
Use the `status` command to check bot health:
```bash
!status  # or /status
```

This shows:
- **Latency**: Connection quality to Discord
- **Voice Status**: Current playback state (Playing/Paused/Idle/Disconnected)
- **Queue**: Number of songs in queue
- **Current Position**: Playback position in current song
- **Servers**: Number of servers the bot is in

### Error Recovery
The bot includes automatic error handling:
- Catches and logs playback errors without crashing
- Handles network failures gracefully
- Provides user-friendly error messages
- Logs detailed stack traces to `bot.log` for debugging

**Note**: If the bot stops playing unexpectedly, check `bot.log` for details and use `!status` to verify connection.

## Troubleshooting

### FFmpeg not found
Ensure FFmpeg is installed and available in your system PATH.

### Bot doesn't respond
- Check that Message Content Intent is enabled in Discord Developer Portal
- Verify the bot has proper permissions in your server
- Check the command prefix matches what's in your `.env` file

### Audio quality issues
Adjust the `format` option in `YTDL_OPTIONS` in `bot.py`.

### Bot disconnects unexpectedly
Check your network connection and Discord API status.

## License
This project is licensed under the GPLv3.
