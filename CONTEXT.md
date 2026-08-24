# Chief of Staff Agent

This context defines the user-facing language for a personal Teams assistant
that can continue work after a chat turn and send later notifications.

## Language

**Live turn**:
An inbound personal Teams message with the signed-in user's context available.
_Avoid_: Session, request

**Briefing**:
A Work IQ-grounded summary of today's calendar and the previous 24 hours of mail
and Teams activity, organized around priorities, meeting preparation, important
decisions, risks, and next actions.
_Avoid_: Digest, report

**Deferred task**:
A user-started operation that continues after its original Teams turn ends
because no answer text arrived before the response threshold.
_Avoid_: Background job, delayed reply

**Proactive notification**:
A new Teams message sent after the Activity that established the conversation
has ended.
_Avoid_: Callback, push

**Installation recipient**:
A personal Teams conversation registered to receive proactive notifications
while the app remains installed.
_Avoid_: Subscriber, owner

**Routine notification**:
Generic text supplied by a Foundry Routine and broadcast to installation
recipients. It does not contain unattended Work IQ data.
_Avoid_: Scheduled briefing
