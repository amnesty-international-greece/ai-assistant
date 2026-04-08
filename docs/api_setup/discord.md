# Discord Bot Setup

## Overview
Discord bot for forum management, announcements, and member verification.

## Steps

### 1. Create Bot Application
1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. "New Application" → Name: `AI Amnesty Bot`

### 2. Create Bot User
1. Go to "Bot" tab
2. Click "Add Bot"
3. Enable intents:
   - Server Members Intent ✓
   - Message Content Intent ✓

### 3. Generate Bot Token
1. Under "Bot" → click "Reset Token"
2. **Copy token immediately**

### 4. Invite Bot to Server
1. Go to "OAuth2" → "URL Generator"
2. Scopes: `bot`, `applications.commands`
3. Bot permissions: Send Messages, Manage Messages, Read Message History, Embed Links
4. Use generated URL to invite bot to the server

### 5. Record Credentials
Add to `.env`:
```
DISCORD_BOT_TOKEN=<bot token from step 3>
DISCORD_GUILD_ID=<right-click server → Copy Server ID>
```
