# Hackathon context: Meta Muse Ember

Shared context for building Beacon, a mobile and web app, for the Meta Muse Ember Hackathon. Every thread working on Beacon should read this first.

## At a glance

- **Goal:** build something ambitious and useful with Muse Ember for under **$10** in Meta Model API credits.
- **Build window:** 2026-11-23 9:00 AM ET to 2026-12-02 **9:00 PM ET** (submission deadline).
- **Model pricing:** Ember is $0.10 per million input tokens, $0.50 per million output tokens.
- **Modalities:** Ember reads text, images, audio, video and PDFs natively.
- **Hard rules:** public repo, solo only, built on Muse Ember through the Meta Model API, stay within $10 (going over means disqualification), new work only.
- **Must submit:** 3-minute demo video, public GitHub repo, project description, and a prompt/token trace (usage screenshot, plus Muse Code trajectory JSON if Muse Code is used).

## Constraints for Beacon

- Every model call goes through Muse Ember on the Meta Model API. Track tokens and spend from day one so the trace is easy to produce.
- Budget is $10 total, and spend-limit alerts only email; they do not stop usage. Guard against runaway loops. Beacon's always-on inputs (radio audio, aircraft positions) spend the most, so cap them in code, not only in a dashboard.
- The repo must be public, and the demo must show only what was built during the hackathon. Any recording replayed in the demo (radio, camera footage) must be one we have the rights to use.
- Avoid the banned project types listed below. In particular, the map is not the main feature (no dashboard apps), photos only add evidence to an incident (no image analyzer), and EMS traffic is summarized without describing the patient (no medical advice).
- Use only data we have rights to: check each source's terms, and ask before using anybody's audio. OpenMHz was asked and said not to use its API, so Beacon requests nothing from it and does not use its recordings; Beacon has no license from Broadcastify and requests nothing from it; and Beacon does not request LiveATC's audio. Publish summaries, never raw radio audio.

## Organizer clarification (Discord, Nov 25)

Asked about a nursing tool, the organizers said the line is who the advice is for, not the topic. A health-related tool may not diagnose, name a condition or its severity, or suggest medication or dosing, and medical image analysis is banned separately. For Beacon this means EMS calls are described as "medical response" with crews and location only, never the patient's condition.

## 01 • Your goal

Build something ambitious and useful with Muse Ember for under $10. Every approved builder gets $10 in Meta Model API credits and 10 days to see how far that budget goes. Ideas to explore:

**Cheap enough to run on everything.** Ember is $0.10 per million input tokens, $0.50 per million out, so one small task costs a fraction of a cent. That makes volume the interesting part. Instead of one careful call on one hand-picked input, you can run the same task over thousands of items. Build something that does that end to end on real data, preferably solving a problem in your own world. The data can be your own corpus, a public dataset, or anything you can get your hands on.

**Beyond text, under budget.** Ember reads text, images, audio, video and PDFs natively. Build something that turns messy real-world input (calls, recordings, screen sessions, photos, stacks of scans) into actions or structured output.

## 02 • Prizes

One project can compete for both prizes.

- **Best Frontier Build: $2,500 USD.** The most impressive, technically ambitious or unexpected project built with Muse Ember.
- **Scale the Real World: $2,500 USD.** Solving a real, messy problem at a scale, speed, or unit cost that only works because of Ember.

## 03 • Getting ready

- **Build window:** November 23 at 9:00 AM ET to December 2 at 9:00 PM ET.
- **Account setup:** create a Meta Model API account at https://developer.meta.com/ai/products/meta-model-api/. A card is required for a refundable holding charge.
- **Credits:** $10 per approved participant, added in batches twice daily (about 10 AM and 3 PM) after approval. Final approvals and credit uploads happen by November 30 (the guide says "Wednesday, November 30", but Nov 30 is a Monday).

## 04 • Community

- Discord: https://cerebralvalley.ai/e/muse-ember-hackathon/discord
- Introduce yourself in #intros. Ask questions in the Ember Muse Hackathon channel and ping @Cerebral Valley.

## 05 • Schedule

- **Nov 23:** 9:00 AM ET hacking begins. 10:00 AM ET credits issued in daily batches.
- **Nov 23 to Dec 2:** build asynchronously.
- **Nov 30:** 12:00 PM ET applications close. Applicants approved after Nov 23 get credits within 24 hours of approval.
- **Dec 2:** 9:00 PM ET submissions due.
- **Dec 3 to 6:** asynchronous judging.
- **Dec 7:** winners announced.

Dates may change at the organizers' discretion.

## 06 • Rules

- **Open source:** repositories must be public.
- **Solo only:** each participant applies individually and submits one project. No team entries.
- **Build on Muse Ember:** through the Meta Model API (Muse Code encouraged, not required), and stay within $10 in credits.
- **Proof of usage:** every submission includes a prompt/token trace showing Muse Ember usage and amount spent.
- **Eligibility:** legal US residents, 18 or older.
- **Prizes:** one project can compete for both.
- **Demo requirements:** the demo must only highlight features, code, and functionality built during the hackathon. Judges must be able to clearly identify what was created during the event, or the project is disqualified.
- **New work only:** no presenting an existing project as your own work.
- **Banned projects:** anything that violates legal, ethical, or platform policies, or uses code, data, or assets you do not have rights to.

### Projects to avoid

- AI mental health advisor
- Basic RAG applications
- Streamlit applications
- Image analyzers
- "AI for Education" chatbot
- AI job application screener
- AI nutrition coach
- Personality analyzers
- Anything using AI to generate and give medical advice
- Any project where a dashboard is the main feature
- Sports analyzers or coaches

## 07 • Meta-provided resources

**Claim credits** (by Nov 30 at 12:00 PM ET, individually): [credits form](https://docs.google.com/forms/d/e/1FAIpQLSe5KAT0_j9f0h_nqR4JONKJ7BVHLNX2dgcR-QYTe9pbkh5zzg/viewform?usp=sharing&ouid=105167472314657374076). Check the Billing page that credits applied. A card must be on the account to make API calls. A spend-limit notification can be set on the Billing page.

**Get started**

- [Build with Muse Ember (launch blog)](https://dev.meta.ai/resources/blog/build-with-muse-ember)
- [dev.meta.ai](http://dev.meta.ai)

**Docs**

- [About Muse Ember](https://dev.meta.ai/docs/muse-ember)
- [Overview](https://dev.meta.ai/docs/overview)
- [Quickstart guide](https://dev.meta.ai/docs/quickstart)
- [Model list](https://dev.meta.ai/docs/models)
- [Muse Code setup](https://dev.meta.ai/docs/muse-code)
- [Muse Code authentication](https://dev.meta.ai/docs/muse-code/auth)

**Cookbook**

- [Muse Ember cookbook](https://dev.meta.ai/docs/cookbook/muse-ember)
- [API cookbook](https://github.com/meta-models/meta-model-cookbook)
- [API fundamentals](https://github.com/meta-models/meta-model-cookbook/tree/main/01_api_fundamentals)
- [Muse Code recipes](https://github.com/meta-models/meta-model-cookbook/tree/main/04_muse_code)

**Track usage**

- [Usage page](https://dev.meta.ai/usage)
- [How usage tracking works](https://dev.meta.ai/help/usage-and-limits/api-usage)
- [Spend-limit alerts](https://dev.meta.ai/help/billing/spend-limit-alert) (the alert only sends an email; it does not stop usage)
- [Reading token usage](https://dev.meta.ai/docs/prompt-caching)

## 08 • Submission

Submit at https://cerebralvalley.ai/e/muse-ember-hackathon/hackathon/submit. Include:

- [ ] 3-minute demo video highlighting what was built during the hackathon
- [ ] Public GitHub repository
- [ ] Project description
- [ ] Usage screenshot: on the [Usage page](https://dev.meta.ai/usage), set dates to Nov 23 to Dec 2, filter to Muse Ember, and capture total tokens and spend
- [ ] If Muse Code was used: run /export, choose Trajectory, and submit the JSON

Double check the repo is public, the demo link is accessible, and the project was built entirely during the hackathon.

## 09 • Judging

One asynchronous round, December 3 to 6.

| Criterion | Weight | What judges look for |
| --- | --- | --- |
| Impact | 30% | Real-world potential, who benefits and how much, whether people would use it, fit with the problem statements |
| Use of Meta AI tooling | 25% | Creative use beyond a basic integration, surfacing surprising capabilities |
| Technical depth | 20% | Sound, thoughtfully refined engineering with real craft |
| Demo | 25% | Working, impressive, holds up live, video is genuinely cool to watch |

## 10 • FAQs

- **Can I use Muse Code?** Yes, encouraged but not required. The project must be built on Muse Ember via the Meta Model API.
- **What if I go over $10?** Overage is your responsibility, and going over means disqualification.
- **When do credits arrive?** Twice daily (about 10 AM and 3 PM) after approval, starting Nov 23.
- **Teams?** No, solo only.
- **Who can participate?** Legal US residents 18 or older.
- **Can one project win both prizes?** Yes.
