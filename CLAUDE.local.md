# Plex Library Helperr

I ultimately want to build out a new web ui portion to control and display all related tools to this.
Would like to be able to control this from the UI which should be behind an auth page (we can start with cloudflare)
Ideally it will be a one stop place for me to see the status of radarr, sonarr, overseerr, sabnzbd and run any helper functions we create. This way i don't need to log onto each individual site to check their status and i can maintaing my library from here.
In addition to that, I want insight into the usage of plex, who requests what, who watches what they request, who has trouble with direct play or other issues, etc. I want to optimize plex and want insight and data into how to get there.

- site will only be used by me or admin access but we should still protect api keys and tokens to a secure and proper standard.
- Should be setup using api endpoints so we can trigger different jobs as needed. Open to other suggestions
- Docker container so i can deploy and update on unraid
- items to add
  - empty trash scheduler: Empty trash automatically after every scan is turned off on my plex server so we can handle trash emptying as needed
  - database for our data (assuming we will need this)
  - user stats:
    - who requested what movie/tv/frequency
    - who watches what they request
    - who watches what (tautulli and tracearr already provide this so we don't need to reinvent the wheel, importing/displaying as needed is ok)
  - dry run option
  - history of events we've done
  - ui should allow us to configure/add/change workers removers etc
