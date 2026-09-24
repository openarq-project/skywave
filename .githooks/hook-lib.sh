#!/bin/sh
# skywave hook library — sourced by commit-msg / pre-commit / pre-push.
#
# A Claude session URL/id is a handle to a private development transcript, so
# it never goes into a commit message or into file content. Co-author trailers
# are fine. The same check as armstrong's .githooks (minus armstrong's private
# pattern list, which is that project's own).
SESSION_PAT='claude\.ai/code/session|^Claude-Session:|session_01[A-Za-z0-9]{20,}'
