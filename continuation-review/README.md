# Continuation review

A registered post-turn plugin that asks TypeSafe Jev whether the exact completed
turn has left an explicitly authorized execution task unfinished. It injects a
single continuation as user input only for `continue`; `stop`, `wait`, invalid
responses, missing credentials, network failures, and refused injections never
resume anything. It does not approve tools, resolve user prompts, or start new
scope.

The hook uses the documented public `post_llm_call` hook and calls
`ctx.inject_message()` synchronously while the hook's session context is live.
It intentionally supplies no guessed session key. Hermes routes that keyless
hook injection to the exact calling session; the gateway's busy-session queue
handles scheduling. A `True` result means accepted for dispatch, not that the
follow-up completed. The injected message is normal conversational input and
cannot approve a pending confirmation.

## Credentials and permission

The plugin reads only `TYPESAFE_API_KEY`, from the process environment or the
explicitly authorized `/home/shi/.hermes/.env` (then the active profile `.env`),
without sourcing the file. Credentials are never logged or included in requests
other than the TypeSafe Authorization header. The key remains outside this repo.

Gateway injection is opt-in and must be enabled in the profile that uses it:

```yaml
plugins:
  entries:
    continuation-review:
      allow_gateway_injection: true
```

The permission allows this plugin to queue conversational messages to authorized
sessions in that profile; it is not an approval bypass.

## Jev policy

The live confidence floor is 0.40. For `continue`, Jev must also assign at least
0.60 probability to continuation and a 0.20 margin over both alternatives. Live
synthetic fixtures verified: an unambiguous unfinished task continued at 0.74
confidence; an answered informational question stopped; an approval wait waited;
and the actual stage-one turn continued at confidence 0.43 with probabilities
`continue=0.62, stop=0.11, wait=0.27`. That calibration allows the observed
valid continuation without accepting a weak/marginal decision. Malformed
answers, low confidence, and transport failures fail closed. Locally known
cancellation, approval, running subagent, or active-session flags also avoid the
API and do not inject.

## Verification

```sh
python3 -m unittest discover -s . -p 'test_*.py' -v
hermes plugins validate .
hermes plugins doctor . --ci
```

Official interfaces: [TypeSafe System One API](https://docs.typesafe.ai/api),
[Hermes full documentation](https://hermes-agent.nousresearch.com/docs/llms-full.txt) sections “Injecting Messages” and `post_llm_call`.
