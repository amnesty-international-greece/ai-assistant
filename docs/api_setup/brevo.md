# Brevo API Setup

## Overview
Brevo (formerly Sendinblue) for newsletter distribution to Section members.

## Steps

### 1. Generate API Key
1. Log in to [Brevo](https://app.brevo.com)
2. Go to "SMTP & API" → "API Keys"
3. Click "Generate a new API key"
4. Name: `AI-in-AI Platform`

### 2. Verify Sender Identity
1. Go to "Senders & IP" → "Senders"
2. Add sender: `info@amnesty.org.gr`
3. Complete domain verification (SPF/DKIM records)

### 3. Set Up Contact Lists
1. Create list: "Board Members" (for board-specific emails)
2. Create list: "All Members" (for newsletters/circulars)
3. Note the list IDs for use in config

### 4. Record Credentials
Add to `.env`:
```
BREVO_API_KEY=<API key from step 1>
```

### 5. Design Templates
Design newsletter templates in Brevo UI:
- Board meeting invitation template
- Meeting reminder template
- General circular template
- Special circular template
- GA invitation template

Note template IDs for use in workflow configuration.
