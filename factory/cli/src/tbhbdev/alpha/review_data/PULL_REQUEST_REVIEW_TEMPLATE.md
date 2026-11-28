<!-- Remove the guidance comments and any unused optional sections. -->

## Summary

<!-- Required. Give the reasons in one paragraph of one or two sentences and
at most 40 words. Put the verdict in the review event. -->

## Findings

<!-- Optional. Use up to eight findings in a bullet list. Start each
with F1: P1 correctness, F2: P2 design, and so on. Count from F1 without gaps.
Kinds are correctness, security, design, test, and docs. Use one or two
sentences of at most 30 words after the kind. P0 means harm or a security
breach, P1 means broken basic use or correctness, P2 means debt worth fixing
soon, and P3 means minor. For an inline comment, cite the changed line as
`path:line`. Posting moves that finding from the body to an inline comment.
Use findings without a file location for issues with the whole PR, and cite a
check and its result when relevant. P0 and P1 require REQUEST_CHANGES. -->

<!-- Name the real situation for each finding. End P1 findings with an
Occurs when: clause. Skip impossible cases at every severity. Flag code that
handles an impossible case or adds an option, layer, or abstraction with one
user. Ask for deletion. From round 2, limit new findings to lines the fix changed. -->

## Suggestions

<!-- Optional. Use up to five bullets starting with S1:, S2:, and so on.
Use at most 25 words after each prefix. -->

## Verification

<!-- Required. Use up to six unnumbered bullets of at most 20 words each.
Describe what you checked and what the evidence showed. -->
