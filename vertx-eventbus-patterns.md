# Vert.x EventBus patterns and the worked KYC example

Static call-graph analysis cannot link a producer to a consumer because the link is a
runtime string address, not a resolved method call. The parser surfaces the resolved
address on both sides so you can join them; flag the joined edge as `llm-inferred`.

## Wiring
- Producer:  eventBus.send(ADDR, body) | request(ADDR, body) | publish(ADDR, body)
- Consumer:  eventBus.consumer(ADDR, handler)  ->  verticle handle(Message<JsonObject>)
- Payload:   message.body()  -> JsonObject requestBody
             requestBody.getString("field") -> String field   (DIRECT/IDENTITY)

## Worked multi-hop example (Lineage 1)
Source dataset: one-data-global-merchant-setup-kyc.$api_payload

1. api_payload.applicationIdentifier
   --[INDIRECT/CONDITIONAL, masking=true]  requestHeaders.remove("AUTHORIZATION") strips auth at entry
2. message.body()
   --[DIRECT/IDENTITY]  materializes EventBus payload as JsonObject requestBody
3. requestBody.getString("applicationIdentifier")
   --[DIRECT/IDENTITY]  ->  $requestBody.applicationIdentifier

Then guard `auditEnabled && isBlank(applicationIdentifier)` (INDIRECT/CONDITIONAL) assembles a
ValidationResponse(status="9", VALIDATION_FAILURE, errors=[applicationIdentifier/KYC000]) and
replies with HTTP_STATUS_CODE=200 — no A2A token fetch or SOR call on this early-error path.
