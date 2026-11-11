# Syntax Academy updates

The signed update feed for [Syntax Academy](https://github.com/gigglenuts34/syntax-academy-updates/releases/latest), Byte's bubble academy for learning 47 programming languages.

Install on Arch / CachyOS:

    curl -LO https://github.com/gigglenuts34/syntax-academy-updates/releases/latest/download/syntax-academy.pkg.tar.zst
    sudo pacman -U ./syntax-academy.pkg.tar.zst

(Download first: pacman only accepts unsigned packages from a local file, not straight from a link.)

The installed app checks `latest.json` here every hour, verifies its Ed25519 signature and the file hashes, and switches to new versions by itself.
