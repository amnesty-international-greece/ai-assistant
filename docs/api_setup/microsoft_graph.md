# Microsoft Graph API Setup

## Overview
Microsoft Graph API provides access to OneDrive (document archival) and potentially Outlook/Exchange.

## Steps

### 1. Register Application in Azure AD
1. Go to [Azure Portal](https://portal.azure.com) → Azure Active Directory → App registrations
2. Click "New registration"
3. Name: `AI-in-AI Platform`
4. Supported account types: "Accounts in this organizational directory only"
5. Redirect URI: `http://localhost:8000/auth/microsoft/callback` (Web)
6. Click "Register"

### 2. Configure API Permissions
1. Go to "API permissions" → "Add a permission"
2. Select "Microsoft Graph" → "Application permissions"
3. Add:
   - `Files.ReadWrite.All` — Upload/download files in OneDrive
   - `User.Read.All` — Read user profiles
4. Click "Grant admin consent"

### 3. Create Client Secret
1. Go to "Certificates & secrets" → "New client secret"
2. Description: `AI-in-AI Platform`
3. Expiry: 24 months
4. **Copy the secret value immediately** (it won't be shown again)

### 4. Record Credentials
Add to `.env`:
```
MS_CLIENT_ID=<Application (client) ID from Overview page>
MS_CLIENT_SECRET=<Secret value from step 3>
MS_TENANT_ID=<Directory (tenant) ID from Overview page>
```

### 5. Verify
Run: `python -m src.cli.commands status` — Microsoft Graph should show ✓
