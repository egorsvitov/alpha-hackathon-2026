# Activity interests v1: editorial guide

This catalogue contains search hypotheses about activities, not verified facts
about individual companies. It does not change retrieval automatically.

## Scope and format

Preserve the supplied `okved_code` and `activity` verbatim. Write exactly five
independent interests per activity, in Russian. Each interest has:

- `slug`: a unique, descriptive ASCII snake_case key within its activity;
- `title`: a short Russian title;
- `description`: one or two concise sentences suitable for embedding directly;
- `applicable_when`: null for a broadly applicable topic, otherwise a short
  condition describing the narrower business model for which it applies.

Use `{"okved_code": "…", "activity": "…", "interests": […]}` per record.
Target 15–35 Russian words per description; meaningful specificity matters more
than length. Name the objects and changes to find: relevant materials, equipment,
services, customer markets, operating constraints or regulatory mechanisms.

## Editorial rules

1. Each of five interests addresses a distinct mechanism. Do not paraphrase one
   idea five times or combine unrelated topics in a single description.
2. Write search content, not instructions such as “искать новости”, “компании
   важно”, “следить за ситуацией”. Avoid empty economy/business/innovation prose.
3. Match the precise activity: installation is not equipment manufacture;
   wholesale is not retail; property management is not property development.
4. Include concrete examples of inputs or services relevant to this activity.
   Broad parent codes may use representative examples without assuming all
   companies engage in every subactivity.
5. Do not assert the company borrows, exports, imports, serves government buyers,
   owns premises or uses particular equipment unless the activity establishes it.
   Use `applicable_when` for genuinely narrower exposures. Do not turn five
   interests into five speculative special cases.
6. Avoid fabricated law numbers, current prices/rates, dates and company names.
   Discuss stable mechanisms; no external factual research or paid API needed.
7. Common mechanisms may recur across activities, but descriptions must explain
   their concrete connection to each activity. Do not generate all records with
   sector-wide string replacement or attach the same five generic themes.
8. Regional, company product and payment data are outside this catalogue.
9. Check for wrong-domain terms, overlapping topics and misleading financial
   assumptions. All output remains agent-authored and not human-approved.

## Independent review

Read every activity and all five descriptions, using the supplied exact activity
name as the scope. Correct substantive errors, generic filler, near duplicates
and unsupported role assumptions. Preserve valid detail. Add review notes with
code, issue, action and any unresolved ambiguity. A review is an agent review,
not independent human relevance labels or an official validation of OKVED.
