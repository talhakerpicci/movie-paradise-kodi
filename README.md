# Movie Paradise for Kodi

Track what you watch in Kodi with [Movie Paradise](https://movieparadise.app).

- Shows what you're playing as **Watching now**, with a live progress bar
- Marks movies and episodes as watched when you finish them
- Keeps your plays when you're offline and sends them once you're back

Works with Kodi 19 (Matrix), 20 (Nexus) and 21 (Omega). Linking an account requires Movie Paradise Premium.

## Installation

Installing the repository is recommended, since Kodi then keeps the add-on up to date.

1. Download [repository.movieparadise-1.0.0.zip](https://github.com/talhakerpicci/movie-paradise-kodi/raw/repo/repository.movieparadise/repository.movieparadise-1.0.0.zip)
2. In Kodi, enable **Settings > System > Add-ons > Unknown sources**
3. Go to **Add-ons > Install from zip file** and pick the downloaded zip
4. Go to **Add-ons > Install from repository > Movie Paradise Repository** and install **Movie Paradise**

You can also install [the add-on zip](https://github.com/talhakerpicci/movie-paradise-kodi/tree/repo/service.movieparadise) directly the same way, without the repository, but then you won't get updates automatically.

## Linking your account

1. Open **Add-ons > Program add-ons > Movie Paradise** and choose **Link**
2. Kodi shows a code. In the Movie Paradise app, go to **Linked devices** and enter it
3. That's it. Play something and it shows up in the app

The same menu (or the add-on settings) shows the link status and lets you unlink.

## What gets tracked

- Movies with a TMDB or IMDb id
- Episodes from your library, and episodes from other add-ons that provide an IMDb id

Live TV, trailers and extras, music videos and stacked files are skipped. Plays from the Plex, Jellyfin and Emby add-ons are skipped by default, since those servers can report to Movie Paradise directly. You can change this in the settings.

A title counts as watched when it plays to the end or once you've watched 90% of it.

## Privacy

The add-on only sends what you play (the title's ids, the position and the time) to Movie Paradise. Your device token is stored in the add-on's profile folder and is never written to Kodi's log.

## Development

The tests run without Kodi (its modules are stubbed):

```sh
python3 -m pytest -q
```

Kodi 19 ships Python 3.8, so it's worth running them on 3.8 too, for example with `uv run --python 3.8 --with pytest python -m pytest -q`.

To build the release zips and the repository index into `dist/`:

```sh
python3 tools/build_release.py
```

The repository add-on reads from the `repo` branch, so publishing a release means copying `dist/` there.

## License

GPL-2.0-or-later, see [LICENSE.txt](LICENSE.txt).
