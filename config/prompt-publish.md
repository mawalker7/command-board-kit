You are the COMMAND BOARD publisher. Non-interactive. Do exactly these steps and nothing else.

1. Read the file `board.doc.json` in the current directory with the Read tool. It is a single line of JSON. Keep its exact text; you will upload it verbatim.
2. Load the Drive tools: ToolSearch `select:mcp__claude_ai_Google_Drive__create_file,mcp__claude_ai_Google_Drive__search_files,mcp__claude_ai_Google_Drive__read_file_content,mcp__claude_ai_Google_Drive__trash_file`.
3. Create the Drive file: title `command-board-data`, contentMimeType `text/plain`, textContent = the exact single-line contents of board.doc.json (no reformatting, no added whitespace, no code fences). Note the returned `id`.
4. Verify the upload: call read_file_content on that id. The returned text is the same JSON with backslashes inserted before punctuation; that is expected. Check that it starts with `{"schema":1` (allowing for those backslashes) and ends with `}` and is not obviously truncated compared with what you read in step 1. If it looks truncated or empty, trash that file and repeat step 3 once. If the second attempt also fails, stop and print `PUBLISH_FAILED: <reason>`.
5. Prune: search `title = 'command-board-data' and owner = 'me'` (pageSize 50). Sort by createdTime descending. Keep the 6 newest; trash the rest with trash_file.
6. Notify: run `osascript -e 'display notification "Board refreshed. Open the Command Board in Claude." with title "Command board ready"'`.
7. Print exactly one final line: `DRIVE_FILE_ID=<the id from step 3>`.

Never modify board.doc.json. Never create any other file. Never print the JSON contents back into the transcript beyond the checks in step 4.
