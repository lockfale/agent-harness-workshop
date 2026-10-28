# Writing observations (sample)

A sample note about how recon output should read.

- An observation is something you can point at: a status code, a header, a
  path, a parameter, a line of a response. "The login form posts to /auth" is
  an observation. "The login is vulnerable" is a conclusion.
- Record what you saw, where, and how you got it (the request), so someone
  else can reproduce it.
- Record negative results too. "No robots.txt (404)" saves the next person a
  request.
- Leave ranking and severity to triage. Recon that ranks its own output
  drops the things it found boring.
