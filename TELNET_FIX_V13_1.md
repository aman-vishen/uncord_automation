# Telnet Fix v13.1

The MAC Writer and Quality Verifier previously passed a Python `str` prompt to `telnetlib3` `readuntil()`. The reader searches a byte buffer and requires a bytes separator.

Both applications now:

1. Encode prompts such as `login:`, `Password:` and `#` to UTF-8 bytes before `readuntil()`.
2. Decode byte responses back to text with replacement for invalid characters.
3. Continue supporting `str` responses from normal `reader.read()` calls.
