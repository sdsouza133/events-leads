"""Where emails go when there's no Resend key: local work and previews (ADR 0004). Each one is kept in the
`outbox_emails` table, for the outbox page, instead of being sent. Production requires the key (registry)."""

from sqlalchemy.dialects.postgresql import insert

from app.db import session_scope
from app.models import OutboxEmail
from app.providers.types import OutgoingEmail


class OutboxMailer:
    def send(self, email: OutgoingEmail) -> None:
        """Keep the email. Like Resend, a key already kept is dropped, so a retried job doesn't keep it twice."""
        with session_scope() as session:
            session.execute(
                insert(OutboxEmail)
                .values(key=email.key, to=email.to, subject=email.subject, text=email.text, html=email.html)
                .on_conflict_do_nothing(index_elements=[OutboxEmail.key])
            )
