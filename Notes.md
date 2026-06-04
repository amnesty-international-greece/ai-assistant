Replace the current email templates with the following ones (threaded under the Συνεδρίαση ΔΣ00-0000 subject):

# **Scheduling Email**

Με σκοπό τον προγραμματισμό και την προετοιμασία της συνεδρίασης υπ’ αριθμόν ΔΣ00-0000, παρακαλούμε συμπληρώστε διαθεσιμότητες και προσθέστε θέματα στην ημερήσια διάταξη **μέχρι τις ΧΧ/ΧΧ**.

//Insert the lettucemeet/doodle url as a link in “διαθεσιμότητες” and the agenda Google Sheet url in “ημερήσια διάταξη”. If no lettucemeet/doodle url is provided, remove “τον προγραμματισμό ” and “συμπληρώστε διαθεσιμότητες και ” from the message above. Replace “ΔΣ00-0000” with the actual meeting id and “XX/XX” with the date four days after the email by default (or whatever date parsed as a CLI argument during initialisation of the process).

# **Second Scheduling Email //in case of no lettucemeet/doodle url**

Παρακαλούμε συμπληρώστε διαθεσιμότητες για την επόμενη συνεδρίαση **μέχρι τις ΧΧ/ΧΧ**.

//Insert the lettucemeet/doodle url as a link in “διαθεσιμότητες”.

# **Invitation Email**

Η επόμενη συνεδρίαση του Διοικητικού Συμβουλίου θα πραγματοποιηθεί στις \[Ημερομηνία\], και ώρα ΧΧ:ΧΧ. Παρακάτω μπορείτε να βρείτε τα στοιχεία σύνδεσης:  
Zoom Link: \[Zoom Link\]  
Meeting ID: \[Meeting ID\]  
Password: \[Password\]

//Insert the OneDrive share link of the official pdf invitation as a link in “συνεδρίαση”. Replace \[Ημερομηνία\] with the date in full greek format like in the official pdf invitation (eg 12 Μαΐου 2026). Replace the \[Zoom Link\], \[Meeting ID\], and \[Password\] according to the Zoom Meeting details (dont just copy the whole Zoom invitation, it contains too much irrelevant info, like how to join from your phone etc).

# **Minutes Email**

Σας κοινοποιείται το προσχέδιο των πρακτικών της προηγούμενης συνεδρίασης. Μπορείτε να σχολιάσετε και να προτείνετε διορθώσεις απευθείας επί του εγγράφου.

//Insert the draft minutes url as a link in “πρακτικών”.

---

Can you also check out the following Discord Message Components features and think of how we can integrate them to make our bot better and more visually elegant?

## 1\. Select Menus (Drop-downs)

Select Menus are interactive dropdown lists that you can attach to messages. They save space and provide a clean way for users to make choices without typing commands. Discord supports multiple types of select menus natively:

* **String Select:** You define a custom list of options (e.g., picking a game mode, selecting a setting).  
* **User & Role Select:** Automatically populates a dropdown with server members or server roles.  
* **Channel Select:** Allows users to pick a specific text or voice channel from the server.  
* **Mentionable Select:** A combined list of users and roles.

## 2\. Modals (Pop-up Forms)

When you need a user to type out information (like submitting a bug report, filling out an application, or providing feedback), doing it in a public chat is messy. **Modals** are true UI pop-ups that appear in the center of the user's screen. They can contain short text inputs and multi-line paragraph boxes. The data is sent securely to your bot without cluttering the chat channel. *(Note: Modals can only be triggered when a user clicks a button, uses a select menu, or runs a command).*

## 3\. Rich Embeds

Embeds are the classic way to make your bot look professional. Instead of plain white text, an embed is a structured card that allows you to bundle information beautifully. You can include:

* **Colored Sidebars:** Match the embed to your bot's branding or the status of the message (e.g., green for success, red for error).  
* **Author Blocks & Footers:** Small text areas for attribution or timestamps.  
* **Thumbnails & Hero Images:** Display images directly inside the card structure.  
* **Fields:** Arrange text in neat, organized columns and rows.

## 4\. Application Commands (Context Menus)

While you likely know about **Slash Commands** (`/`), you can also add your bot directly to Discord's right-click menus.

* **Message Context Commands:** When a user right-clicks a message and goes to "Apps", they can trigger your bot to act on that specific message (e.g., "Translate this message" or "Report this message").  
* **User Context Commands:** When a user right-clicks someone's profile, they can trigger your bot to act on that user (e.g., "View server profile" or "High-five user").

## 5\. Ephemeral Messages ("Only you can see this")

When a user interacts with your bot (like clicking a button or running a command), you don't always want the bot's reply to be visible to the entire server. You can send an **Ephemeral Message**, which is a temporary message that only the interacting user can see. It keeps channels incredibly clean while still providing immediate feedback to the user.

## 6\. Discord Activities (Embedded Web Apps)

If you are building something highly interactive (like a multiplayer game, a shared whiteboard, or a complex dashboard), bots can now launch **Activities**. This allows your bot to open a sandboxed iframe directly inside a Discord voice channel or text chat, running standard web code (HTML/CSS/JS) as a native-feeling application.