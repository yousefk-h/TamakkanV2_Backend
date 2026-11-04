# Tamakkan project access (Task 5)

Status: complete for the agreed branch-based development setup. Verified against
GitHub's authenticated API on 2026-09-17.

## Verified access

The GitHub account authenticated on Yazan's development machine is
`YZN3MRKSJO`. GitHub reports read and write access to both private repositories:

| Repository | Read | Write | Working branch |
| --- | --- | --- | --- |
| Backend (GitHub name pending rename, plan step 7) | Yes | Yes | the legacy integration branch |
| Frontend (GitHub name pending rename, plan step 7) | Yes | Yes | the legacy integration branch |

Both remote branches exist. Both local checkouts use the legacy integration branch and track
their corresponding remote branch. No additional invitation is
needed for this account. The account does not have repository administrator or
maintainer permissions; these are not needed to contribute code.

## Project names and review workflow

The repositories kept their legacy GitHub names at the time, so a rename was not
required. Tamakkan remains the platform's configured visual identity.

Develop and submit changes on the legacy integration branch. The senior reviewer handles
approval and merging into `main`, as agreed with the team. The existing deployment
workflows target `main`; they are separate from the local Tamakkan setup.

This access check did not commit or push the current working changes, grant new
permissions, or rename repositories. Permission to write does not mean the local
implementation has been published.

## Application and hosted access

Local startup and test-account instructions are in
[docs/LOCAL_DEVELOPMENT.md](docs/LOCAL_DEVELOPMENT.md). GitHub access and application
login are separate; local application passwords are kept in the ignored
`.tamakkan/local/accounts.json` file.

The agreed setup has no separate hosted Tamakkan deployment yet. Access to its
hosting project, service accounts, and hosted application must be configured and
verified when those resources are provisioned. Existing GitHub permissions do not
establish access to those services.
