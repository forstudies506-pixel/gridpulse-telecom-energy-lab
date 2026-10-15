import Foundation
import ImageIO
import UniformTypeIdentifiers

let input = URL(fileURLWithPath: "assets/logo.png")
let output = URL(fileURLWithPath: "assets/logo.webp")
guard let source = CGImageSourceCreateWithURL(input as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil),
      let destination = CGImageDestinationCreateWithURL(output as CFURL, UTType.webP.identifier as CFString, 1, nil)
else {
    exit(1)
}
CGImageDestinationAddImage(destination, image, nil)
guard CGImageDestinationFinalize(destination) else { exit(1) }
