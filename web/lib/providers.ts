/**
 * Written walkthroughs for providers we have documented by hand.
 *
 * This is an *enhancement layer*, not the list of what can be connected. The
 * credentials page is driven by the database, so all 120 indexed providers are
 * reachable; for the ones below we can additionally show step-by-step
 * instructions, and for everything else the page falls back to what the
 * specification declares plus the provider's own documentation link.
 *
 * An earlier version of this file *was* the list, which quietly made 110
 * providers unreachable — the same mistake as building for the handful you
 * happen to have in mind rather than for the corpus that exists.
 */

export type ProviderGuide = {
  id: string;
  name: string;
  ready: boolean;
  scheme: "bearer" | "apikey" | "basic";
  tokenUrl?: string;
  steps?: string[];
  note?: string;
  /** Connects through the OAuth sign-in flow rather than a pasted token. */
  oauth?: string;
  /** One-time setup the user must complete before the flow can run. */
  oauthSetup?: string[];
};

export const PROVIDER_GUIDES: ProviderGuide[] = [
  {
    id: "github_com",
    name: "GitHub",
    ready: true,
    scheme: "bearer",
    tokenUrl: "https://github.com/settings/personal-access-tokens",
    steps: [
      "Open github.com and go to Settings (your avatar, top right)",
      "Scroll to the bottom of the left sidebar and click Developer settings",
      "Click Personal access tokens, then Fine-grained tokens",
      "Click Generate new token, give it a name and an expiry date",
      "Under Repository access pick the repositories you want to automate",
      "Under Permissions grant Issues: Read and write (add others as needed)",
      "Click Generate token and copy the value — it is shown only once",
    ],
    note: "The token starts with github_pat_ (fine-grained) or ghp_ (classic). Both work.",
  },
  {
    id: "slack_com",
    name: "Slack",
    ready: true,
    scheme: "bearer",
    tokenUrl: "https://api.slack.com/apps",
    steps: [
      "Go to api.slack.com/apps and click Create New App, then From scratch",
      "Name it, pick your workspace, and click Create App",
      "In the left sidebar open OAuth & Permissions",
      "Under Bot Token Scopes click Add an OAuth Scope and add chat:write",
      "Scroll up and click Install to Workspace, then Allow",
      "Copy the Bot User OAuth Token (it begins xoxb-)",
      "In Slack, invite the bot to your channel: /invite @YourAppName",
    ],
    note: "The final invite step matters — without it Slack returns not_in_channel.",
  },
  {
    id: "stripe_com",
    name: "Stripe",
    ready: true,
    scheme: "bearer",
    tokenUrl: "https://dashboard.stripe.com/test/apikeys",
    steps: [
      "Sign in to dashboard.stripe.com",
      "Make sure the Test mode toggle (top right) is ON",
      "Open Developers, then API keys",
      "Reveal and copy the Secret key (it begins sk_test_)",
    ],
    note: "Use the test key. Test mode never moves real money, which makes Stripe the safest provider to experiment with.",
  },
  {
    id: "sendgrid_com",
    name: "SendGrid",
    ready: true,
    scheme: "bearer",
    tokenUrl: "https://app.sendgrid.com/settings/api_keys",
    steps: [
      "Sign in to app.sendgrid.com",
      "Open Settings, then API Keys",
      "Click Create API Key, name it, and choose Restricted Access",
      "Grant Mail Send permission",
      "Click Create & View and copy the key (shown only once)",
    ],
  },
  {
    id: "trello_com",
    name: "Trello",
    ready: true,
    scheme: "apikey",
    tokenUrl: "https://trello.com/power-ups/admin",
    steps: [
      "Go to trello.com/power-ups/admin and create a Power-Up",
      "Open its API key page and copy the API key",
      "Click the Token link next to the key and approve access",
      "Copy the token that is shown",
    ],
    note: "Trello needs both a key and a token; paste the token here.",
  },
  {
    id: "asana_com",
    name: "Asana",
    ready: true,
    scheme: "bearer",
    tokenUrl: "https://app.asana.com/0/my-apps",
    steps: [
      "Go to app.asana.com/0/my-apps",
      "Click Create new token under Personal access token",
      "Name it, accept the terms, and copy the token",
    ],
  },
  {
    id: "twilio_com",
    name: "Twilio",
    ready: true,
    scheme: "basic",
    tokenUrl: "https://console.twilio.com",
    steps: [
      "Sign in to console.twilio.com",
      "On the dashboard find Account SID and Auth Token",
      "Paste them here joined by a colon: ACxxxx:your_auth_token",
    ],
    note: "Twilio uses basic authentication, so the value is SID:TOKEN rather than a single token.",
  },
  {
    id: "telegram_org",
    name: "Telegram",
    ready: true,
    scheme: "bearer",
    tokenUrl: "https://t.me/BotFather",
    steps: [
      "Open Telegram and start a chat with @BotFather",
      "Send /newbot and follow the prompts to name your bot",
      "BotFather replies with a token like 123456789:AAF-xxxxxxxxxxxxxxxx",
      "Copy that token and paste it below",
    ],
    note: "Telegram puts the token in the URL itself (api.telegram.org/bot<token>/...) rather than in a header, so although the token stores correctly, this engine cannot execute Telegram calls yet — it builds addresses from indexed metadata and does not substitute into the host.",
  },
  {
    id: "httpbin_org",
    name: "httpbin (testing)",
    ready: true,
    scheme: "bearer",
    steps: [
      "No account needed — httpbin echoes whatever you send it",
      "Paste any text as the token, for example: test",
    ],
    note: "The safest way to watch a real request go out and come back without touching a live service.",
  },
  {
    id: "googleapis_com",
    name: "Google (Gmail, Calendar)",
    ready: true,
    scheme: "bearer",
    oauth: "google",
    tokenUrl: "https://console.cloud.google.com/apis/credentials",
    oauthSetup: [
      "Go to console.cloud.google.com and create a project (or pick one)",
      "Open APIs & Services, then Library, and enable the Gmail API and the Google Calendar API",
      "Open APIs & Services, then OAuth consent screen; choose External and fill in the required fields",
      "On the Audience/Test users step, add your own Google address as a test user",
      "Open APIs & Services, then Credentials, and click Create credentials, OAuth client ID",
      "Choose Web application, and under Authorised redirect URIs add exactly: http://localhost:3000/oauth/callback",
      "Click Create and copy the Client ID and Client secret",
      "Put them in .env as GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET, then restart the API",
    ],
    note: "This is a one-time setup. Google will not issue a token any other way — an API key cannot read your mail, because that needs your explicit approval.",
  },
  {
    id: "whatsapp_local",
    name: "WhatsApp Business",
    ready: false,
    scheme: "bearer",
    note: "The indexed WhatsApp specification is the self-hosted On-Premise Business API: you run Meta's containers yourself and sign in with a username and password against your own server, which is why its address is whatsapp.local. There is no OAuth flow and no hosted endpoint to connect to — the prerequisite is infrastructure, not a token. Meta has since replaced it with the Cloud API, which is not in this corpus.",
  },
];

export function guideFor(providerId: string): ProviderGuide | undefined {
  return PROVIDER_GUIDES.find((guide) => guide.id === providerId);
}
