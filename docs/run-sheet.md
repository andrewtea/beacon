# Run sheet: the three-minute video

This is for whoever films Beacon's demo video. For each scene it says what to open, what to press, what appears, how long that takes, and one sentence to say that is true. It is written from what was seen on Nov 28, 2026, on the server [`scripts/film`](../scripts/film) runs, with Muse Ember telling the invented story ([README, "Showing the story"](../README.md#showing-the-story)). Two things in it were seen only on a stand-in's telling of the same story, and each says so.

## What the video shows, and what it does not

- **The incident is invented.** It is a made-up three-alarm fire on the 400 block of Hayes St. Its radio calls are synthesized speech of invented words (two of them, at minutes 5 and 21, are generated noise in which nothing is said), and its notices are invented PDFs. Every page says "Invented scenario", and the video says so in words in scene 2.
- **Muse Ember does the model's work, for real.** It hears each call, reads each notice, picks the points of the incident's story, and investigates each tap. The tokens, times, and costs on the trace are the ledger's.
- **A tap is sent from an invented place**, "Near the invented fire, Hayes Valley". The filming server takes nothing of a visitor's own, as the public deployment does not: no place, sound, photo, or notification address. So the video does not show the phone's microphone, its camera, or a notification arriving. Say nothing that suggests it does.
- **No real radio is in it.** Do not say any of these, which the build plan's scene table had and which are not true: "a full day of San Francisco radio", "thousands of transmissions", "minutes ahead of the record", or any accuracy of Beacon on real radio.

## Before filming

One telling of the story with Muse Ember is paid for, call by call. The first telling from minute 12 to the end, with two taps, cost $0.018 (50 requests). Each of the seven taps Muse Ember has investigated on the story cost between about $0.0007 and about $0.0013 (the least was $0.000688, the most $0.001326), in three or four requests.

1. **Read what the ledger holds, and compare it with Meta's Usage page.** This opens the ledger so that it cannot be written to, makes nothing, and fails with "unable to open database file" when no ledger is at the path:

   ```sh
   python3 -I - /home/user/workspace/repo/.amp/in/orb-backup/paid-data/ember/ledger.sqlite3 <<'EOF'
   import sqlite3, sys
   ledger = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
   count, nanousd = ledger.execute(
       "select count(*), coalesce(sum(cost_nanousd), 0) from ember_calls where source = 'live'"
   ).fetchone()
   print(count, "live requests, $%.6f" % (nanousd / 1e9))
   EOF
   ```

   On Nov 28, 2026 it printed 635 live requests and $0.118714. If it fails, or Meta's [Usage page](https://dev.meta.ai/usage) shows spend, for every model, that the ledger and `EMBER_SPENT_BEFORE_USD` together do not account for, stop: nothing is paid until that is settled. Do not use `python -m beacon.ember` for this look. It asks Muse Ember nothing, but it begins an empty ledger in a data directory that has none, which is the ledger `scripts/film` refuses to begin.
2. **Bring the server up.** In the Amp thread that holds the ledger, send: "Begin the filming server's story anew at minute 0." Or run this in that thread's Terminal, from a checkout of `main`:

   ```sh
   BEACON_DATA_DIR=/home/user/workspace/repo/.amp/in/orb-backup/paid-data \
     BEACON_MOCK_EMBER=live REPLAY=0 \
     BEACON_MOCK_EMBER_BUDGET_USD=0.40 BEACON_SCALE_RUN=ember-sample-1 \
     scripts/film anew 0
   ```

   **`EMBER_SPENT_BEFORE_USD` is left out of the command on purpose.** The script reads it from the environment, gives it no default, and is refused without it. It is what Meta's [Usage page](https://dev.meta.ai/usage) shows as spent against the credit, for every model, beyond what this ledger holds, and [nobody has reported reading that page](../README.md#the-spending-stop). It is what step 1 compares. The variable is `0` only when the page shows no more than the ledger. A project's orb has the project's value for it in its environment; when the page says otherwise, change that value where it is set, since the script does not start a server whose shell and orb disagree.

   After its looks at the settings, which change nothing, the command stops the filming server, moves the last telling's store whole under `told/` in the data directory, builds the app, starts the server, and prints the portal's address and the clock time of every moment of the story. The last telling's evidence, stories, and investigations are kept as files. What the server had noted as sent to devices about that telling's incidents is forgotten; no device has given this server an address. `BEACON_DATA_DIR` is the one data directory that has been paid from: its ledger counts every earlier request. Do not point the command at another.
3. **Open the portal's address on the phone**, signed in to Amp. It is HTTPS and opens for whoever can open the thread.
4. **Read the clock times.** `scripts/film where`, with the same settings, prints them again and says which minute the story is in. The story runs by the clock, once, for 95 minutes, and cannot be held or moved: only `anew` begins it again, and each `anew` is paid for.

The story's minutes, for a telling begun at minute 0:

| Minute | What comes | Scene |
| --- | --- | --- |
| 0 | Dispatch to a structure fire, 400 block of Hayes St | |
| 3 | Engine 36 on scene, smoke showing | 2: one tap, between minute 3 and minute 6 |
| 4.2 | A working fire is declared | 2: the card changes by itself |
| 6 | Second alarm | Do not tap again until minute 21 |
| 9 | 511 lists Hayes St as closed | |
| 14 | An evacuation of the building next door is mentioned on the radio | |
| 16 | A shelter-in-place warning for smoke is issued | |
| 18 | Third alarm | |
| 19 | The evacuation order is published as a PDF | It is on the page once Muse Ember has read it: 84 seconds later, the one time this was timed |
| 21 to 30 | | 4: the second tap |
| 74 | The order is lifted, in a second PDF | |
| 78 | "All clear" on the radio | |
| 90 | The last units are cleared | 2, last shot, from minute 91: the record is closed |

**Why one tap before minute 6, and none from then to minute 21.** Crisis mode is decided when a tap near the incident is investigated, only the first decision for an incident turns it on, and a decision stands on any one signal the incident has. Two things give the incident a signal before the evacuation order exists:

- **The story, from minute 6.** The second alarm gives it an alarm level and nine units, and the closure and the helicopter follow. In the two tellings that were watched the incident had no signal before the second alarm was heard, and neither tap sent before minute 6 turned crisis mode on. That is what Muse Ember heard those two times, and it can hear a call otherwise.
- **Taps, at any minute.** Three taps from near an incident are a signal of their own, so once three have come, a decision can stand on that alone, whatever the story has said. Send one tap before minute 6, and a second only if the first has to be done again.

If crisis mode came on at an earlier tap, scene 4's tap does not turn it on: the decision that is kept is the earlier one, with the signals it cited then, which the panel lists under "What the decision rested on". To film crisis mode coming on, begin the story anew.

## The scenes

Times are the build plan's. What appears is quoted from the app.

### 1. The button (0:00 to 0:20)

- **Open:** the portal's address. Film the first screen.
- **Appears:** "Beacon", the tag "Invented scenario", the lines "The incidents, alerts, and notices here are made up." and "Nobody has checked what the hosting service this server is reached through writes down of a request.", and the What's that? button.
- **Say:** "You hear a siren and wonder what is going on. Beacon is one button: What's that?"

### 2. One tap, one answer (0:20 to 0:55)

- **When:** story minute 3 to 6. For the card to change on camera, tap about half a minute before minute 4.2.
- **Press:** What's that?
- **Appears:** "Looking into it" for four to fifteen seconds, then the card: "Fire at the 400 block of Hayes St", "Open in Beacon's record", "Invented", "The radio said: units on scene.", the units named on the radio (B2, E21, E36, T6), the first radio call's time, and under it "Medium confidence" and "Generated radio calls, synthesized speech and no radio, as Ember heard them". Scroll to "Why this incident" and the timeline: each line has its confidence and its source.
- **Then:** the card asks again by itself, with no press. A change shows some time after its moment in the story, because Muse Ember hears the call first: the one time this was timed, the card said "The radio said: crews at work." 37 seconds after minute 4.2. After minute 6 it lists nine units and "Alarm level on the radio 2".
- **Last shot, filmed after minute 91:** open the same answer again: under "Your taps" on the button's page, press the time of the tap. The card says "Closed in Beacon's record". (Seen on a stand-in's telling.)
- **Say:** "This fire is invented. The voices on its radio are synthesized; no real radio was used. Muse Ember heard each call, and when I tap, it investigates and answers in seconds: what, which units were named on the radio, which block, with a source and a confidence on every line."
- **Say over the last shot:** "When the radio clears the call, Beacon's record of it closes."

### 3. The agent trace (0:55 to 1:15)

- **Press:** "See each step of the investigation", on the answer.
- **Appears:** "How Beacon investigated this tap", "Beacon's investigator is a model, Muse Ember. Its reading can be wrong.", then the totals (requests answered, tokens in and out, time, cost), then each round: "Answered by Muse Ember", its tokens, time, and cost, the tool it called and why, and what the tool found. Both times a tap between minute 3 and minute 4 was tried it was two rounds and an answer: `nearby_incidents` first, and then `get_official`, once by itself and once with `get_radio` and `nearby_taps`.
- **Say:** "Here are the tool calls kept for this tap, and the accounting of each request Ember answered: its tokens, its time, and its cost. Ember chose which tools to call, and code checked its answer." Then read the cost off the screen: it was $0.000797 and $0.000688 the two times.
- **Do not say** that Ember pulled a helicopter's track: at minute 3 the story has no helicopter, and Ember called no aircraft tool in the runs that were seen.

### 4. Crisis mode (1:15 to 1:50)

- **When:** story minute 21 to 30, with no tap sent since minute 6.
- **Press:** "Beacon" at the top, then What's that? again.
- **Appears:** "Looking into it" again (eight seconds, the time this was filmed), then the card, now with twelve units and "Alarm level on the radio 3", and under it the panel "Crisis mode is on": "Beacon's investigator turned on crisis mode for this incident, because of the signals the incident had at that time. That is the investigator's decision, not an authority's. Beacon relays what authorities publish and tells nobody what to do." Scroll to "From authorities": the "Shelter In Place Warning" as an alert in force, and the "Evacuation order", "Published", shown as "Beacon's reading of the document, not the authority's words" with "Read the document" beside it. Under "Key points": "An evacuation was mentioned on the radio. That is radio traffic, not an official order."
- **Film the order to the foot of its card**, where "Invented notice, as Ember read it", the tag "Invented", and "Read the document" are. The instruction in a frame by itself reads as a real order.
- **Then, for the saved place:** press "Open your places" on the button's page, then "Save 'Near the invented fire, Hayes Valley'". It says "Beacon has this incident about 100 m from this place."
- **Say:** "Later in the story it is a third alarm. Ember's investigation of this tap turned crisis mode on: its decision, which code checked against the incident's own signals. The invented evacuation order is shown as a published document, as Beacon's reading of it, with its link, and the evacuation mentioned on the radio is labeled as radio traffic, not an order."
- **Say over the saved place:** "A saved place's name stays on the phone. Beacon is sent where it is, rounded to about a block, and keeps nothing of that. Here it is an invented place. Beacon says how far the incident is from it, and never whether to stay or go."
- **Its trace, as an extra shot:** the time this was filmed it was three requests for $0.001113: `nearby_incidents`, then one round of `get_official`, `get_radio`, `get_aircraft`, and `nearby_taps`, then the answer. Say that Ember checked the aircraft only if `get_aircraft` is on the screen.
- **Do not say** that a notification arrives. Notifications are built and tested against stand-ins for the push services; none has been delivered to a phone, and the filming server takes no notification address.
- **If filmed after minute 78:** the incident's page has "Where sources disagree", which says the official source holds over the all-clear that was reported on the radio. (Seen on a stand-in's telling.)

### 5. Scale (1:50 to 2:40)

- **Open:** "Scale run results", at the foot of any page. On the filming server it is Muse Ember's paid run of a sample. A desktop browser shows more of it at once.
- **Appears:** what a scale run is, then what this run is ("Generated speech, for Muse Ember to hear. ..."), then each number with its sentence and the tag of the run's kind beside it. The ones to stop on: "DataSF calls in the sample" (100 calls), "Transmissions heard" (288 calls), "Cost of hearing" ($0.0345), "Cost per answered investigation, mean" ($0.0005), "Time per answered investigation, median" (4.5 s), and under "Glossary", "Codes the glossary added, in agreement with DataSF" (5 codes).
- **Say:** "For scale, Beacon generated speech from DataSF's public record of one day of fire calls. Muse Ember was paid to hear a sample, the first hundred calls of that day, 288 transmissions, for three and a half cents. A script replayed 34 taps against them, and an investigation cost a twentieth of a cent on average and took Ember a median of four and a half seconds. This is clean synthetic speech and not real radio, and the page says so beside every number."
- **Say, for the glossary:** "From a table of counts alone, Ember proposed what five unit codes nobody had written down stand for, and all five agreed with DataSF's unit types. A model can also simply know a fire department's codes."
- **For the whole day, optional:** the public deployment's results page (<https://beacon-sf--andrewtea.onamp.dev/#/results>) is a stand-in's run of all of that day's 1,525 generated calls. **Say:** "A stand-in ran the whole day through the same pipeline. That checks the pipeline and measures nothing of Ember."

### 6. Cost (2:40 to 3:00)

- **Open:** the trace of scene 3 again, at its totals, or a terminal with the ledger's total: the read-only look of "Before filming", step 1, which prints the ledger's live requests and their cost and writes nothing. That total is what this ledger holds, and is the account's spend only if Meta's Usage page shows no more.
- **Not for the video:** the token trace the submission needs is written by `python -m beacon.ember` (`ember/trace.jsonl` in the data directory). Give it only the data directory whose ledger the read-only look has just shown: in a directory with no ledger it begins an empty one.
- **Say:** "Everything Muse Ember has done for Beacon, every call heard, notice read, story, and investigation, has cost" and then the cost the look printed. On Nov 28, 2026, after 625 requests, it was $0.115236. "Telling this fire once, from its twelfth minute to its end, cost under two cents."

## When something differs

- **Ember's hearing differs from telling to telling.** Of the same synthesized words it once heard "100 block" for "400 block". If a card shows something the story does not say, that is the model's hearing, shown as such: film it, or begin the story anew.
- **The answer is "unconfirmed".** Read the sentence the page gives with it. It can be the investigator's reading (it found no incident near, or could not tell that the tap is about one), a match that code did not bear out, or no reading at all: no answer Beacon could use, or an investigation cut short. Film it as what it says, or tap once more, and mind the taps: three taps from near the incident are a signal by themselves, and from minute 6 any tap can turn crisis mode on.
- **Crisis mode did not come on at the second tap.** It is Ember's decision and it can decide otherwise. Each of the three times a first tap from minute 6 on was tried, it turned it on. Tap again.
- **Crisis mode came on at a tap before scene 4's.** The decision that is kept is that first one. "What the decision rested on", in the panel, lists the signals it cited as they were then: read them there, and do not tell them from what the page showed at the time, which asks the server by itself. To film crisis mode coming on, begin the story anew.
- **The orb was paused.** The story went on by the clock. `scripts/film where` says where it is; `scripts/film anew 0` begins it again.
- **`scripts/film` refuses or fails.** A refusal that comes of its looks at the settings says "Nothing was stopped, moved, or started", and that is so. `anew` can also fail after them: with the server stopped, with the store moved only in part (status 3, and the sentence says so), or with the store moved and the app not built or the server not started. Before another `anew`, see what is there: `scripts/film where` says whether a server answers. A move left in part is finished by giving the same `anew` again, and until then no server starts on the data directory. After a move that finished, `scripts/film up 0` starts the server on the new story without setting anything more aside. The script never begins a ledger and never changes a spending setting.
