<?php
// Read-only archive viewer: hide every action that would compose, send, or
// mutate the archive. The :ro Maildir mount already makes the mutating actions
// (delete/move/copy/mark/purge) fail at the IMAP layer, but leaving the buttons
// live means a household user clicks them and gets an opaque error -- disabling
// them keeps the UI honest about what the viewer can do.
$config['disabled_actions'] = [
    'compose',
    'reply',
    'reply-all',
    'reply-list',
    'forward',
    'forward-inline',
    'forward-attachment',
    'bounce',
    'delete',
    'move',
    'copy',
    'mark',
    'purge',
    'expunge',
];
$config['smtp_host'] = '';
