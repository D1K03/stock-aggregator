/* How Rupert works, as the `/rupert/how` page draws it.

   **The diagrams here and the ones in `docs/rupert.md` are the same strings**,
   and `tests/test_rupert_docs.py` fails if they stop being. The doc renders on
   GitHub and this renders in the dashboard; two copies that quietly disagreed
   would be worse than one copy in a place only half the readers can reach.

   Generated from the doc once and edited there since — if you change a diagram,
   change `docs/rupert.md` and re-run the check. */

export type HowSection = {
  title: string;
  /** The mermaid source, or null for a section that is prose only. */
  diagram: string | null;
  paragraphs: string[];
};

export const HOW_RUPERT_WORKS: HowSection[] = [
  {
    title: "1. The decision path",
    diagram: "flowchart TD\n    item[\"one item<br/>a Reddit comment or an article\"]\n\n    item --> regex{\"regex shortlist<br/>cashtags + bare CAPS<br/>against the universe\"}\n\n    regex -->|\"no candidates<br/><b>94.4% of the corpus</b>\"| nothing[\"no row, no request<br/>the frontier still moves\"]\n    regex -->|\"more than 8 candidates\"| crowded[\"<b>crowded</b><br/>a list, not a claim<br/>never sent\"]\n    regex -->|\"1-8 candidates<br/><b>5.6% of items</b>\"| jev\n\n    jev[\"<b>Jev</b> typesafe/jev-1.13<br/>one call, five questions, in parallel<br/>~$0.00002\"]\n\n    jev --> inj{\"injection > 0.5?\"}\n    inj -->|yes| none1[\"<b>none</b><br/>refused before it is believed\"]\n    inj -->|no| which{\"which company?\"}\n\n    which -->|\"chose <i>none</i>\"| none2[\"<b>none</b><br/>ordinary English, or the<br/>market rather than a company\"]\n    which -->|\"a company\"| conf{\"confidence >= 0.9?\"}\n\n    conf -->|\"below the floor\"| unsure[\"<b>unsure</b><br/>kept with its whole distribution<br/>so the threshold can be re-cut\"]\n    conf -->|\"at or above\"| lookup{\"is it a current symbol?\"}\n\n    lookup -->|no| failed[\"<b>failed</b><br/>the provider changed something\"]\n    lookup -->|yes| resolved[\"<b>resolved</b><br/>linked to a security\"]\n\n    resolved --> finbert[\"<b>FinBERT</b> /score<br/>batched 32 at a time<br/>three probabilities, never one number\"]\n    finbert --> reading[\"rupert.reading\"]\n\n    classDef out fill:#fff,stroke:#e5e7eb,color:#77716c\n    classDef good fill:#fdeae5,stroke:#e75532,color:#b75000\n    classDef warn fill:#fef3c6,stroke:#f99c00,color:#dd7400\n    class nothing,none1,none2 out\n    class resolved,reading,finbert good\n    class unsure,crowded,failed warn",
    paragraphs: [
      "One item goes in. Most of the time nothing comes out, and that is the layer working rather than failing.",
      "**Why there is no blacklist.** Bare-token matching pulls in ordinary English: over three days the top 25 matches included `YOU` (113), `ON` (104), `IT` (82), `ARE` (63), `ALL` (47), `AM` (45), `NOW` (44) and `PM` (43) \u2014 beside real traffic in `MU` (346), `SNDK` (214), `AMD` (157) and `NVDA` (125). Dropping the first group would discard Allstate, Gartner and ON Semiconductor permanently. A word list cannot tell *\"I put it ALL on calls\"* from *\"ALL reported a combined ratio of 91\"*, because the difference is the sentence. So the regex shortlists and something that can read the sentence chooses.",
      "**Why the gates are `noul` and the choice is not.** An independent calibration test measured this model **overconfident on `choice`** out of distribution (refit temperature 3.29) and **under-confident on `noul`** (0.66). Erring low is safe for a gate and dangerous for a link, so the yes/no questions guard and the choice carries a high floor with its distribution kept.",
    ],
  },
  {
    title: "2. The five questions, asked in one call",
    diagram: "flowchart LR\n    state[\"<b>state</b><br/>the text + the candidates<br/>and nothing else\"]\n\n    state --> q1[\"<b>which</b> \u00b7 choice<br/>one company, or <i>none</i>\"]\n    state --> q2[\"<b>own_business</b> \u00b7 noul<br/>the company, or the market?\"]\n    state --> q3[\"<b>position_talk</b> \u00b7 noul<br/>a trade, or a claim?\"]\n    state --> q4[\"<b>injection</b> \u00b7 noul<br/>is it steering the reader?\"]\n    state --> q5[\"<b>claim_kind</b> \u00b7 choice<br/>earnings, guidance, product,<br/>legal, management, ownership,<br/>market, chatter\"]\n\n    q1 --> row[\"one row in rupert.mention\"]\n    q2 --> row\n    q3 --> row\n    q4 --> row\n    q5 --> row",
    paragraphs: [
      "All of them are evaluated in parallel, so asking more costs tokens and not a round trip.",
      "The state carries the text and the shortlist and **nothing else** \u2014 not the thread, the subreddit, the score or the author. The published guidance is that accuracy falls as the state fills with content unrelated to the decision, and the author's karma is not evidence about which company a sentence is about.",
      "`claim_kind` is the narrative layer. It feeds a flag, deliberately not a score.",
    ],
  },
  {
    title: "3. Where it sits",
    diagram: "flowchart LR\n    subgraph ingest[\"already built, and stopping short on purpose\"]\n        reddit[\"screener.reddit<br/>social_item\"]\n        magpie[\"screener.magpie<br/>magpie.document\"]\n    end\n\n    subgraph rupert[\"screener.rupert\"]\n        cand[\"candidates<br/><i>pure</i>\"]\n        decide[\"decide<br/><i>httpx only</i>\"]\n        store[\"store<br/><i>psycopg only</i>\"]\n        reduce[\"reduce<br/><i>pure</i>\"]\n    end\n\n    subgraph tables[\"rupert schema\"]\n        mention[(\"rupert.mention\")]\n        readingt[(\"rupert.reading\")]\n        progress[(\"rupert.progress\")]\n    end\n\n    reddit --> cand\n    magpie --> cand\n    cand --> decide\n    decide -->|\"OpenRouter<br/>/api/alpha/decisions\"| store\n    sentiment[\"screener.sentiment<br/>FinBERT\"] --> store\n    store --> mention\n    store --> readingt\n    store --> progress\n\n    mention --> reduce\n    readingt --> reduce\n    reduce -.->|\"not wired yet\"| scoring[\"screener.scoring<br/>Sentiment pillar<br/>at weight 0\"]\n\n    mention --> page[\"/rupert<br/>the review surface\"]\n    readingt --> page",
    paragraphs: [
      "The dashed edge is the whole remaining question. `reduce` is pure and **nothing consumes it**: a new input moves a pillar for every ticker on the night it lands, and the diff step reads a universe-wide shift as a universe-wide set of crossings. It goes in behind a weight-version bump, not beside one.",
    ],
  },
  {
    title: "4. The tables",
    diagram: "erDiagram\n    social_item ||--o| mention : \"resolved from\"\n    document ||--o| mention : \"resolved from\"\n    security ||--o{ mention : \"linked to, when resolved\"\n    mention ||--o{ reading : \"read by a named model\"\n\n    mention {\n        text state \"resolved none unsure crowded failed\"\n        text candidates \"an array: what it chose between\"\n        text chosen\n        numeric confidence \"threshold this, do not trust it\"\n        jsonb probabilities \"so the threshold can be re-cut\"\n        numeric own_business\n        numeric position_talk\n        numeric injection\n        text claim_kind \"the narrative layer\"\n        numeric cost_usd \"the meter\"\n        timestamptz observed_at \"when we decided\"\n    }\n\n    reading {\n        numeric positive\n        numeric negative\n        numeric neutral\n        text model \"part of the key, so a re-read is a row\"\n    }\n\n    progress {\n        text corpus\n        timestamptz read_through \"the frontier\"\n        bigint items_read\n    }",
    paragraphs: [
      "**Two clocks, and they are not the same clock.** A mention carries the item's own timestamp (when the comment was written) and its `observed_at` (when we decided). Activity and cost are keyed on `observed_at`; tone and attention on the item's day. One clock for both would make a backlog being drained look like a day of feverish posting.",
      "**`rupert.progress` is the frontier, not `max(observed_at)` over the mentions.** 94.4% of the corpus produces no row at all, so an hour in which nobody named a ticker would be indistinguishable from an hour nobody looked at \u2014 the argument `screener.edgar` makes about `max(filed_date)`, applied to a scalar instead of a set.",
    ],
  },
  {
    title: "5. What it costs",
    diagram: "flowchart LR\n    a[\"~19,300 items a day\"] -->|\"regex, free\"| b[\"~1,080 shortlisted<br/>5.6%\"]\n    b -->|\"$0.000021 each\"| c[\"~$0.02 a night\"]\n    c --> d[\"<b>~$0.50 a month</b>\"]",
    paragraphs: [
      "Against the project's ~\u00a35\u201310/month target that is comfortable, but one busy night still approaches Steven's entire daily allowance for one person \u2014 so it keeps **its own counter**, `RUPERT_DAILY_MAX_CALLS`, on `screener.magpie`'s precedent. One counter for the assistant and for a corpus pass would let a busy night on Reddit silence the bot.",
      "It defaults to **0**, which is both the cap and the off switch. This is the only thing in the tree that spends money per item rather than per question from a person, so it ships off.",
    ],
  },
  {
    title: "6. What the corpus can actually support",
    diagram: null,
    paragraphs: [
      "The number worth knowing before the pillar is wired. Over three days:",
      "A Sentiment pillar would be `Absent` for roughly **96% of the universe**. That is a fact about the corpus rather than a fault, and it is the reason the pillar has to land at `weight = 0` \u2014 `blend` excludes a zero-weighted pillar from both the blend and `min_coverage`. Whether it belongs as a weighted pillar at all, rather than as a crowding flag on `event_flag_daily`, is still open.",
    ],
  },
];
