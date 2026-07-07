# Zoom API Setup

## Overview
Zoom Server-to-Server OAuth for scheduling meetings and retrieving recordings/transcripts.

## Steps

### 1. Create Server-to-Server OAuth App
1. Go to [Zoom Marketplace](https://marketplace.zoom.us)
2. "Develop" → "Build App" → "Server-to-Server OAuth"
3. App name: `AI-in-AI Platform`

### 2. Configure Scopes
Required scopes:
- `meeting:write:admin` - Schedule meetings
- `recording:read:admin` - Access recordings
- `user:read:admin` - Read user info

### 3. Activate App
1. Complete all required fields
2. Click "Activate"

### 4. Record Credentials
Add to `.env`:
```
ZOOM_ACCOUNT_ID=<Account ID from app credentials>
ZOOM_CLIENT_ID=<Client ID>
ZOOM_CLIENT_SECRET=<Client Secret>
```
