# 4. Sign in with a one-time link to a work email

Date: 2026-10-23 · Status: accepted

## Context

Every run spends real money, so we need to know who is asking and stop abuse. Passwords add storage risk, reset flows
and friction for a free tool someone uses once. Social sign-in doesn't prove the person works at the company.

## Decision

Requesters sign in with a one-time link sent to their email. The email must be on the same registrable domain as the
website they submit, and free-mail domains are refused. Tokens are random, stored as SHA-256 hashes, expire after 30
minutes and work once. Operators sign in the same way; their addresses are listed in `ADMIN_EMAILS`.

## Consequences

- Nothing to leak: no passwords stored.
- Proves the person controls a mailbox at the company, which is also the cap key (one free run per domain).
- Depends on email delivery (Resend, with SPF and DKIM on our domain). Locally and in previews, the link is logged
  instead of sent.
