{
  description = "flake for myocardio4";

  # Nixpkgs / NixOS version to use.
  inputs.nixpkgs.url = "nixpkgs/nixos-24.11";

  outputs = { self, nixpkgs }:
    let

      # to work with older version of flakes
      lastModifiedDate = self.lastModifiedDate or self.lastModified or "19700101";

      # Generate a user-friendly version number.
      version = builtins.substring 0 8 lastModifiedDate;

      # System types to support.
      supportedSystems = [ "x86_64-linux" "x86_64-darwin" "aarch64-linux" "aarch64-darwin" ];

      # Helper function to generate an attrset '{ x86_64-linux = f "x86_64-linux"; ... }'.
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;

      # Nixpkgs instantiated for supported system types.
      nixpkgsFor = forAllSystems (system: import nixpkgs { inherit system; overlays = [ self.overlay ]; });

      pkgs = forAllSystems (system: nixpkgs.legacyPackages.${system});

    in

    {

      devShells = forAllSystems
        (system: {
          default = pkgs.${system}.mkShellNoCC
            {
              packages = with pkgs.${system}; [
                rustc
                cargo
                rust-analyzer
                gcc
                sqlite
                rustfmt
              ];
            };
        });
    };
}
