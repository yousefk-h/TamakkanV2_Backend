# Task 6: initial Tamakkan lessons

Status: waiting for identification of the first two lessons and their source material. No lesson records or student assignments were created during this review.

## Existing documents found

These files are tracked in the frontend repository and were already part of the original repository (commit `3bf7bbb`, 2026-08-19). `Tamakkan-Consolidated-Platform-Plan.md` describes them as the three existing authored lesson sources. Neither that provenance nor the reviewed document text identifies Tamakkan's required first two lessons or their order.

| Document | Authored content present |
| --- | --- |
| `Daily Routine 1.docx` | One video link; morning, afternoon, evening and bedtime speaking questions; target expressions; a 3–4 minute speaking assignment; suggested listening, shadowing and practice schedule. |
| `Talking about Music.docx` | One video link; warm-up and guided questions; grouped phrases; vocabulary/collocations; sentence starters and examples. |
| `Is Travelling the Best Way to Learn About Cultures (1) (2).docx` | Two video links; travel/culture questions; explicitly B2 phrase material; themed vocabulary and examples. |

The review extracted document text for content identification. It did not verify current video availability or evaluate the documents' page layout. Do not treat a filename containing `1`, Git order or alphabetical order as an approved curriculum sequence.

## Material needed

Ask the course owner for the first two lesson files or links, their intended order, target level and any required videos or exercises. They can instead confirm which of the existing documents above should be used and in what order. No passwords or service keys are needed for that clarification.

Suggested message:

> Could you send Tamakkan's first two course lessons, in order, with their target level and any video links or worksheets? I found Daily Routine, Talking about Music and Travelling/Cultures in the original repository. Please confirm whether any of those are the lessons intended for Tamakkan.

## Setup once identified

1. Map the approved source material into existing topic fields: title, level, videos, questions, useful phrases, vocabulary, sentence starters and practice schedule. Preserve authored content and distinguish any newly proposed teaching additions.
2. Create exactly those two topics in the isolated `tamakkan_dev` environment. Retain a source-to-topic mapping so reruns do not produce duplicates.
3. Configure lesson-specific tutor guidance and opening questions. Use the existing transcript generation/review process for video content; never present worksheet text as a verified video transcript or fabricate an approved state.
4. Place the lessons in a calendar in the supplied order and assign it to the local Tamakkan test student. Check the calendar's unlock rules when choosing a test start date.
5. Verify teacher editing and student visibility, vocabulary and video access, ordered lesson availability, and phone/tablet presentation. Verify live conversation and report generation when dedicated Tamakkan service credentials are configured.

The current application already has topic creation/update, calendar templates and plan assignment. The next dependency is curriculum identification, not replacement lesson content.
