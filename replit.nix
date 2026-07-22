{ pkgs }: {
  deps = [
    pkgs.python310
    pkgs.ffmpeg
    pkgs.aria2
    pkgs.megatools
    pkgs.cacert
    pkgs.p7zip
    pkgs.git
    # yt-dlp has required an external JS runtime to solve YouTube's
    # signature/n-param challenge since 2025.11.12 — without one, format
    # URLs still get extracted but the actual googlevideo download 403s.
    # Node (not Deno) is what's provisioned here to match Akbots/ytdl.py's
    # explicit `js_runtimes: {"node": {}}` opt and the Dockerfile's nodejs
    # install (originally added for the separate bgutil PO-token server).
    # Also required by Akbots/bgutil_bootstrap.py, which clones/builds/
    # starts that same PO-token server directly from Python on boot here —
    # Replit doesn't run Dockerfile/entrypoint.sh, so nothing else would.
    pkgs.nodejs_20
    # RAR support (archive.py's /unzip) needs the proprietary `unrar` tool,
    # which lives in nixpkgs' unfree set. Uncomment BOTH lines below if you
    # need RAR extraction on Replit — p7zip alone already covers zip/7z/
    # tar/gz/bz2/xz.
    # pkgs.unrar
  ];
  env = {
    # NIXPKGS_ALLOW_UNFREE = "1";  # required if you uncomment pkgs.unrar above
    #
    # NOTE: pkgs.chromium removed — this Repl's nixpkgs channel builds it
    # from source (pulseaudio/libasyncns etc.) and fails/hangs. Not needed
    # anyway: Akbots/headless.py's playwright fallback self-installs its
    # own chromium binary on first use (see requirements.txt), though on
    # Replit specifically that binary is likely to still fail to actually
    # LAUNCH even once downloaded — it needs OS-level shared libraries
    # (libnss3, libgbm1, libasound2, etc.) that Replit's base image doesn't
    # ship. Everything that depends on headless.py degrades gracefully
    # (falls through to the next fallback) when it's unavailable, so the
    # bot still runs fine on Replit without it.
  };
}
