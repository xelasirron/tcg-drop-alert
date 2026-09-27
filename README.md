# TCG Drop Alert

Checks Pokémon TCG product pages about once a minute, around the clock, on GitHub's free servers. When something goes live you get an **urgent phone push** (via the free ntfy app) and an **email**, with a button that opens the page so you can get into the queue.

It alerts when:

- **IN STOCK / PRE-ORDER LIVE**: the page shows a buy button or the shop marks the item available
- **QUEUE IS OPEN**: Pokémon Center (or another shop) shows its virtual-queue / waiting-room page
- **SPOTTED**: text you're watching for appears on a search page (for products not listed yet)
- a page that used to be 404 comes to life

You get one alert when it goes live, then up to 3 reminders 20 minutes apart while it stays live. If it sells out and comes back, you get alerted again.

---

## Setup (about 15 minutes, no coding)

### 1. Phone alerts (ntfy)
1. Install **ntfy** from the App Store or Google Play.
2. Tap **+** and subscribe to a topic. Make the name hard to guess, e.g. `alex-tcg-7Hq29x`. Anyone who knows the name can read it.
3. In the app's settings for that topic, allow **urgent/max priority** notifications to override Do Not Disturb if you want them to wake you.

### 2. Email alerts (Gmail)
1. Turn on 2-Step Verification for your Google account (you probably already have it).
2. Go to **myaccount.google.com/apppasswords**, create an app password called "TCG alert", and copy the 16 characters.

### 3. Put the bot on GitHub
1. Create a free account at github.com if you don't have one.
2. Click **New repository**, name it `tcg-drop-alert`, set it to **Public**, and create it.
   *Public keeps it free: GitHub gives unlimited run time to public repos. Your passwords stay private (step 4). Only the product list is visible.*
3. Click **uploading an existing file** and drag in everything from this folder, **including the `.github` folder**. (If the `.github` folder won't drag in, use **Add file → Create new file**, type `.github/workflows/monitor.yml` as the name, and paste in the file's contents.) Then click **Commit changes**.

### 4. Add your secrets
In the repo, go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Name | Value |
|---|---|
| `NTFY_TOPIC` | your topic name from step 1 |
| `GMAIL_ADDRESS` | alex.v.norris@gmail.com |
| `GMAIL_APP_PASSWORD` | the 16-character app password |
| `EMAIL_TO` | *(optional)* a different address or several, comma-separated. Defaults to your Gmail. |

### 5. Add the product(s)
Edit `products.yaml` on GitHub (click the file, then the pencil icon). For each product, paste the URL, give it a name, and change `enabled: false` to `enabled: true` (or delete that line). Commit.

### 6. Switch it on and test
1. Open the **Actions** tab. If asked, click **I understand my workflows, go ahead and enable them**.
2. Click **TCG drop alert → Run workflow**. This runs one quick check.
3. Open the run and expand **Watch products**. Each product should say `OUT_OF_STOCK`, `NOT_LISTED`, `IN_STOCK` and so on. If one says `BLOCKED` or `UNKNOWN`, see below.
4. After that it runs by itself every few minutes. Each run keeps checking for about 5h40m, so there's no real gap in coverage.

To test that alerts reach you, run `python monitor.py --test-notify` on your PC with the same settings in the environment. Or temporarily add a product that's definitely in stock, wait for the alert, then remove it.

---

## Things to know

- **Timing:** GitHub sometimes starts scheduled runs late, but because each run lasts almost 6 hours, checks normally happen every ~60 seconds. On a known drop day, you can press **Run workflow** yourself as a backup.
- **If a site blocks the bot:** big shops (Pokémon Center UK especially, and sometimes Argos) use bot protection that can block cloud servers. The bot recognises this: the log shows `BLOCKED`, and after 10 blocked checks in a row it sends you a "Can't see…" warning so you don't rely on it without knowing. If that happens:
  - run the same script on your own PC, where home internet is rarely blocked: install Python, then run `pip install -r requirements.txt`, then `python monitor.py --loop 600` with your secrets set as environment variables;
  - and/or sign up for the shop's own email alerts and follow Pokémon Center UK's social channels as a second source.
- **Wrong status on a page?** Add phrases from that exact page:
  ```yaml
  - name: "Some ETB"
    url: "https://..."
    in_stock_text: ["Add to Basket"]
    out_of_stock_text: ["Coming Soon"]
  ```
- **Product not on the site yet?** Use `mode: text_appears` on a search results page with the set's name in `watch_text` (there's an example in `products.yaml`).
- **Pause everything:** Actions → TCG drop alert → **⋯ → Disable workflow**.
- GitHub pauses scheduled workflows after 60 days with no repo activity. The bot's own state commits normally count as activity, but if it ever stops, re-enable it in the Actions tab.
- This only notifies you. Buying and queueing are done by you, which is also what shop terms allow.
