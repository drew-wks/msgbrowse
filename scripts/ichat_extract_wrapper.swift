import Foundation

// Decode the embedded NSFileWrapper used by older .ichat attachments.
guard CommandLine.arguments.count == 3 else {
    fatalError("expected input and output paths")
}
let input = URL(fileURLWithPath: CommandLine.arguments[1])
let output = URL(fileURLWithPath: CommandLine.arguments[2])
let serialized = try Data(contentsOf: input)
guard let wrapper = FileWrapper(serializedRepresentation: serialized),
      wrapper.isRegularFile,
      let payload = wrapper.regularFileContents else {
    fatalError("embedded attachment is not a regular file")
}
try payload.write(to: output, options: .atomic)
