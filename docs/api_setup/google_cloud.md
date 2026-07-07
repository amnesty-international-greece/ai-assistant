# Google Cloud API Setup

## Overview
Google Cloud APIs provide access to Google Drive (templates), Google Sheets (agenda, decisions), and Gmail (board emails).

## Steps

### 1. Create Google Cloud Project
1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create new project: `ai-in-ai-platform`

### 2. Enable APIs
In "APIs & Services" → "Enable APIs":
- Google Drive API
- Google Sheets API
- Gmail API

### 3. Configure OAuth Consent Screen
1. Go to "OAuth consent screen"
2. User type: Internal (if using Google Workspace) or External
3. App name: `AI-in-AI Platform`
4. Scopes: `drive.readonly`, `spreadsheets`, `gmail.send`

### 4. Create OAuth Credentials
1. Go to "Credentials" → "Create credentials" → "OAuth client ID"
2. Application type: Desktop app (for CLI auth flow)
3. Download JSON → save as `data/google_credentials.json`

### 5. Record Credentials
Add to `.env`:
```
GOOGLE_CLIENT_ID=<from downloaded JSON>
GOOGLE_CLIENT_SECRET=<from downloaded JSON>
GOOGLE_PROJECT_ID=ai-in-ai-platform
```

### 6. First Authentication
Run the platform - it will open a browser window for OAuth consent on first use.
The token will be saved to `data/google_token.json` for subsequent use.
