# What the live runs taught

- Retrieval ranked the shipping section above Returns because "return" did not match "Returns". Fixed with light stemming; a test pins it.
- The store holds draft and archived products. Product search is now `status:active` only.
- The first live run produced two statements with no figure in them that the figure check passed: "the tracking details were emailed to you" (nothing said an email was sent) and "the payment will be returned" after a cancellation (the policy never says so). The guard and prompt now refuse unsupported outcomes.
- A model writing "two business days" got past a digit-only check. Number words followed by a unit are now figures too.
