# YouTube Music Festival Playlist Generator

This Python script reads a text file full of band names, automatically finds their **Top 3 most popular songs** natively on YouTube Music, and creates a brand new public playlist on your personal YouTube account containing all of those tracks! 

It's completely free to use and bypasses Spotify's new Premium-only developer API restrictions.

## Prerequisites

You need Python installed on your computer. You also need to install the required Python libraries.
Open your terminal or command prompt and run:
```bash
pip install ytmusicapi google-auth-oauthlib google-api-python-client
```

## Step 1: Creating a Google Cloud Console Project
The `ytmusicapi` handles all the song searching without needing an API key. However, to actually **create a playlist** on your personal YouTube account and add songs to it, the script needs to use the official Google YouTube Data API.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and log in with the exact Google Account connected to your YouTube Music profile.
2. In the top blue header bar, click the dropdown menu (it might say "Select a Project").
3. A popup will appear. Click **NEW PROJECT** in the top right corner.
4. Name your project something like "Festival Playlist Generator". You can leave "Location" as No Organization. Click **CREATE**.
5. Wait a few seconds for it to create. Once done, make sure that new project is selected in the top dropdown menu.

## Step 2: Enabling the YouTube Data API v3
Now we need to tell this new project that it is allowed to talk to YouTube.
1. In the Google Cloud Console, click the hamburger menu (three horizontal lines) in the top left corner.
2. Go to **APIs & Services > Library**.
3. You will see a search bar. Type in **"YouTube Data API v3"** and press Enter.
4. Click on the result that says exactly **"YouTube Data API v3"**.
5. Click the big blue **Enable** button. It might take a minute to process.

## Step 3: Setting Up the OAuth Consent Screen 
Because your script is accessing your personal, private YouTube data (like your playlists), Google requires you to set up an "OAuth Consent Screen". This is the page that asks for your permission to link the app to your account.

*Note: Since you are only using this script for yourself, we will configure this as an unverified "Testing" app. It is perfectly safe.*

1. In the Google Cloud Console, use the left-hand menu to go to **APIs & Services > OAuth consent screen**.
2. Under "User Type," select **External** and click **Create**.
3. **App Information:** 
   * **App Name:** Type `Playlist Bot` (or anything you want).
   * **User Support Email:** Select your own email address from the dropdown.
   * Scroll to the very bottom to **Developer Contact Information**, and type your own email again.
   * Click **Save and Continue**.
4. **Scopes:** You can completely skip this screen. Just click **Save and Continue**.
5. **Test Users (CRITICAL STEP):** Because your app is in "Testing" mode, Google will *block* anyone from logging in unless their email is explicitly whitelisted here. 
   * Click **+ ADD USERS**.
   * Type in the exact Google Email Address you use for YouTube.
   * Click **Save**, and then click **Save and Continue**.
6. **Summary:** Scroll to the bottom and click **Back to Dashboard**.

## Step 4: Creating and Downloading the `client_secrets.json` File
Now you need the actual "key" that lets your Python script identify itself as the app you just created.

1. In the Google Cloud Console left menu, go to **APIs & Services > Credentials**.
2. Click **+ CREATE CREDENTIALS** at the top top of the page.
3. Select **OAuth client ID** from the dropdown menu.
4. Under "Application Type", select **Desktop app**.
5. Change the "Name" to something memorable like `Python Script OAuth`. Click **Create**.
6. A popup will appear saying "OAuth client created." 
7. Click the **DOWNLOAD JSON** button (it looks like a down arrow with a bracket under it). 
8. Move that downloaded file into the exact same folder as your `bot.py` script.
9. **Rename the file** to exactly `client_secret.json`. 

*(Be sure your script looks for this exact filename. If you named it something else, you will need to open `bot.py` and update the `YOUTUBE_CLIENT_SECRETS_FILE` variable!)*

## Step 5: Giving the Script its First Test Run
You are ready to go! Ensure `bands.txt` contains your list of bands, one per line.

1. Open a terminal or command prompt in your working directory.
2. Run the script:
   ```bash
   python bot.py
   ```
3. A new tab will instantly open in your web browser. 
4. Select your Google account.
5. Google will show a big scary warning screen saying **"Google hasn't verified this app"**. This is completely normal because you haven't paid Google to formally review your personal script!
   * Click **Advanced** at the bottom.
   * Click **Go to Playlist Bot (unsafe)**.
   * Click **Continue** on the next screen to grant permissions.
6. The browser will say "The authentication flow has completed."
7. Look back at your terminal. The script will be running! 

It has automatically saved a new file called `token.json` in your folder. The next time you run `python bot.py`, it will magically load your saved credentials and bypass the browser login entirely!
