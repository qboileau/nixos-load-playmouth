{
  description = "NixOS Plymouth boot splash themes — progressive lambda reveal and spinning ASCII logo";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      lib = nixpkgs.lib;

      supportedSystems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = lib.genAttrs supportedSystems;

      # Single source of truth for every variant: source SVG, animation style
      # and the animation constants the Plymouth script is built with.
      variants = builtins.fromJSON (builtins.readFile ./variants.json);
      variantNames = builtins.attrNames variants;

      # Just what the frame generators read, so frames/ stays out of the store.
      generatorSrc = lib.fileset.toSource {
        root = ./.;
        fileset = lib.fileset.unions [
          ./generate-frames.py
          ./generate-spin-frames.py
          ./variants.json
          ./assets
        ];
      };

      # Build rasterized frames from a source SVG (for CI/regeneration).
      # The committed frames/ PNGs are what the themes actually ship.
      mkFrames = pkgs: variant:
        let
          v = variants.${variant};
          svg = "${generatorSrc}/assets/${v.svg}";
        in
        pkgs.runCommand "nixos-loading-frames-${variant}" {
          nativeBuildInputs = [
            (pkgs.python3.withPackages (ps: [ ps.pillow ]))
            pkgs.librsvg
          ];
        } (''
          mkdir -p "$out"
        '' + (if v.style == "spin" then ''
          export SPIN_FONT=${pkgs.dejavu_fonts}/share/fonts/truetype/DejaVuSansMono.ttf
          python3 ${generatorSrc}/generate-spin-frames.py "${svg}" "$out"
        '' else ''
          python3 ${generatorSrc}/generate-frames.py "${svg}" "$out"
        ''));

      # Shared builder: takes a variant name and produces a Plymouth theme
      # derivation from the pre-rasterized PNGs in frames/ — no build-time
      # SVG tooling.
      mkTheme = pkgs: variant:
        let v = variants.${variant}; in
        pkgs.stdenv.mkDerivation {
          pname = "nixos-loading-plymouth-${variant}";
          version = "0.1.0";
          src = ./.;

          dontConfigure = true;
          dontBuild = true;

          installPhase = ''
            runHook preInstall

            themedir=$out/share/plymouth/themes/nixos-loading-${variant}
            mkdir -p "$themedir"

            # Copy pre-rasterized PNGs
            cp frames/${variant}/*.png "$themedir/"

            # Install Plymouth script with the variant's animation constants
            substitute theme/nixos-loading.script "$themedir/nixos-loading.script" \
              --replace-fail '@num_frames@' '${toString v.numFrames}' \
              --replace-fail '@ticks_per_step@' '${toString v.ticksPerStep}' \
              --replace-fail '@full_logo_frame@' '${toString v.fullLogoFrame}'

            # Install .plymouth config with store path substituted
            substitute theme/nixos-loading.plymouth \
              "$themedir/nixos-loading-${variant}.plymouth" \
              --replace-fail '@themedir@' "$themedir" \
              --replace-fail '@description@' '${v.description}'

            runHook postInstall
          '';

          meta = with pkgs.lib; {
            description = "NixOS Plymouth theme (${variant}) — ${v.description}";
            license = licenses.mit;
            platforms = platforms.linux;
          };
        };

      # GIF preview builder for a variant. Uses the pre-composed animation
      # frames directly — just composites each onto a dark background and
      # assembles the GIF at the variant's animation speed.
      mkPreview = pkgs: variant:
        let
          v = variants.${variant};
          last = toString (v.numFrames - 1);
        in
        pkgs.runCommand "nixos-loading-preview-${variant}" {
          nativeBuildInputs = [ pkgs.imagemagick ];
        } ''
          mkdir -p "$out" work

          # Use a canvas tall enough for the frames (512 logo + 50 spacing + ~16 text)
          W=640
          H=700

          # Composite each animation frame onto a dark background
          for f in $(seq 0 ${last}); do
            convert -size ''${W}x''${H} xc:"#191924" \
              ${./frames}/${variant}/frame-$f.png -gravity center -composite \
              work/frame-$f.png
          done

          # Assemble animated GIF (loop forever)
          delays=""
          for f in $(seq 0 ${last}); do
            delays="$delays -delay ${toString v.previewDelay} work/frame-$f.png"
          done
          convert $delays -loop 0 "$out/preview-${variant}.gif"
        '';

      # { "<prefix><variant>" = builder pkgs variant; } for every variant
      forAllVariants = pkgs: prefix: builder:
        lib.genAttrs (map (n: "${prefix}${n}") variantNames)
          (name: builder pkgs (lib.removePrefix prefix name));
    in
    {
      packages = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        # Themes, rasterized frames (for CI/regeneration) and preview GIFs
        forAllVariants pkgs "nixos-loading-" mkTheme
        // forAllVariants pkgs "frames-" mkFrames
        // forAllVariants pkgs "preview-" mkPreview
        // {
          # Default package is the blue gradient variant
          default = mkTheme pkgs "default";
        }
      );

      devShells = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShell {
            packages = with pkgs; [
              (python3.withPackages (ps: [ ps.pillow ]))  # SVG splitting + frame compositing
              librsvg      # rsvg-convert for SVG → PNG rasterization
              imagemagick  # convert for GIF preview generation
              dejavu_fonts # monospace face for the spin variant's ASCII art
              fontconfig   # fc-match, how generate-spin-frames.py finds it
              plymouth     # local theme testing
            ];
          };
        }
      );

      # NixOS module — configurable variant
      nixosModules.default = { config, pkgs, lib, ... }:
        let
          cfg = config.boot.plymouth.nixos-loading;
        in
        {
          options.boot.plymouth.nixos-loading = {
            enable = lib.mkEnableOption "NixOS loading Plymouth theme";
            variant = lib.mkOption {
              type = lib.types.enum variantNames;
              default = "default";
              description = "NixOS loading Plymouth theme variant.";
            };
          };

          config = lib.mkIf cfg.enable {
            boot.plymouth = {
              enable = true;
              theme = "nixos-loading-${cfg.variant}";
              themePackages = [
                self.packages.${pkgs.stdenv.hostPlatform.system}."nixos-loading-${cfg.variant}"
              ];
            };
          };
        };
    };
}
