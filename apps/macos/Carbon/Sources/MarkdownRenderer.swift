import SwiftUI
import AppKit

// MARK: - Mathematical Expression Typography Formatter

public enum MathFormatter {
    private static let superscriptMap: [Character: Character] = [
        "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
        "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
        "+": "⁺", "-": "⁻", "=": "⁼", "(": "⁽", ")": "⁾",
        "n": "ⁿ", "i": "ⁱ", "x": "ˣ", "y": "ʸ"
    ]

    private static let subscriptMap: [Character: Character] = [
        "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄",
        "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉",
        "+": "₊", "-": "₋", "=": "₌", "(": "₍", ")": "₎",
        "a": "ₐ", "e": "ₑ", "i": "ᵢ", "j": "ⱼ", "k": "ₖ",
        "m": "ₘ", "n": "ₙ", "o": "ₒ", "p": "ₚ", "r": "ᵣ",
        "s": "ₛ", "t": "ₜ", "u": "ᵤ", "v": "ᵥ", "x": "ₓ"
    ]

    private static let symbolReplacements: [(String, String)] = [
        ("\\times", "×"),
        ("\\cdot", "·"),
        ("\\div", "÷"),
        ("\\pm", "±"),
        ("\\mp", "∓"),
        ("\\leq", "≤"),
        ("\\le", "≤"),
        ("\\geq", "≥"),
        ("\\ge", "≥"),
        ("\\neq", "≠"),
        ("\\ne", "≠"),
        ("\\approx", "≈"),
        ("\\equiv", "≡"),
        ("\\propto", "∝"),
        ("\\infty", "∞"),
        ("\\in", "∈"),
        ("\\notin", "∉"),
        ("\\subset", "⊂"),
        ("\\subseteq", "⊆"),
        ("\\cup", "∪"),
        ("\\cap", "∩"),
        ("\\forall", "∀"),
        ("\\exists", "∃"),
        ("\\nexists", "∄"),
        ("\\rightarrow", "→"),
        ("\\to", "→"),
        ("\\leftarrow", "←"),
        ("\\Rightarrow", "⇒"),
        ("\\Leftarrow", "⇐"),
        ("\\Leftrightarrow", "⇔"),
        ("\\alpha", "α"),
        ("\\beta", "β"),
        ("\\gamma", "γ"),
        ("\\Gamma", "Γ"),
        ("\\delta", "δ"),
        ("\\Delta", "Δ"),
        ("\\epsilon", "ε"),
        ("\\zeta", "ζ"),
        ("\\eta", "η"),
        ("\\theta", "θ"),
        ("\\Theta", "Θ"),
        ("\\lambda", "λ"),
        ("\\Lambda", "Λ"),
        ("\\mu", "μ"),
        ("\\pi", "π"),
        ("\\Pi", "Π"),
        ("\\rho", "ρ"),
        ("\\sigma", "σ"),
        ("\\Sigma", "Σ"),
        ("\\tau", "τ"),
        ("\\phi", "φ"),
        ("\\Phi", "Φ"),
        ("\\omega", "ω"),
        ("\\Omega", "Ω"),
    ]

    /// Format mathematical text by transforming LaTeX exponents, subscripts, and symbols into readable typography.
    public static func formatMathText(_ raw: String) -> String {
        var text = raw

        // Replace common LaTeX symbols
        for (latex, unicode) in symbolReplacements {
            text = text.replacingOccurrences(of: latex, with: unicode)
        }

        // Replace \sqrt{...} with √( ... )
        if let sqrtRegex = try? NSRegularExpression(pattern: #"\\sqrt\{([^}]+)\}"#, options: []) {
            let range = NSRange(text.startIndex..<text.endIndex, in: text)
            text = sqrtRegex.stringByReplacingMatches(in: text, options: [], range: range, withTemplate: "√($1)")
        }

        // Replace \frac{a}{b} with (a / b)
        if let fracRegex = try? NSRegularExpression(pattern: #"\\frac\{([^}]+)\}\{([^}]+)\}"#, options: []) {
            let range = NSRange(text.startIndex..<text.endIndex, in: text)
            text = fracRegex.stringByReplacingMatches(in: text, options: [], range: range, withTemplate: "($1 / $2)")
        }

        // Replace bracketed superscripts ^{...}
        if let supBracketRegex = try? NSRegularExpression(pattern: #"\^\{([^}]+)\}"#, options: []) {
            let nsString = text as NSString
            let matches = supBracketRegex.matches(in: text, options: [], range: NSRange(location: 0, length: nsString.length)).reversed()
            for match in matches {
                let inner = nsString.substring(with: match.range(at: 1))
                let converted = String(inner.map { superscriptMap[$0] ?? $0 })
                text = (text as NSString).replacingCharacters(in: match.range, with: converted)
            }
        }

        // Replace single character superscripts ^c
        if let supSingleRegex = try? NSRegularExpression(pattern: #"\^([0-9a-zA-Z+-])"#, options: []) {
            let nsString = text as NSString
            let matches = supSingleRegex.matches(in: text, options: [], range: NSRange(location: 0, length: nsString.length)).reversed()
            for match in matches {
                let charStr = nsString.substring(with: match.range(at: 1))
                if let char = charStr.first, let sup = superscriptMap[char] {
                    text = (text as NSString).replacingCharacters(in: match.range, with: String(sup))
                }
            }
        }

        // Replace bracketed subscripts _{...}
        if let subBracketRegex = try? NSRegularExpression(pattern: #"\_\{([^}]+)\}"#, options: []) {
            let nsString = text as NSString
            let matches = subBracketRegex.matches(in: text, options: [], range: NSRange(location: 0, length: nsString.length)).reversed()
            for match in matches {
                let inner = nsString.substring(with: match.range(at: 1))
                let converted = String(inner.map { subscriptMap[$0] ?? $0 })
                text = (text as NSString).replacingCharacters(in: match.range, with: converted)
            }
        }

        // Replace single character subscripts _c
        if let subSingleRegex = try? NSRegularExpression(pattern: #"\_([0-9a-zA-Z+-])"#, options: []) {
            let nsString = text as NSString
            let matches = subSingleRegex.matches(in: text, options: [], range: NSRange(location: 0, length: nsString.length)).reversed()
            for match in matches {
                let charStr = nsString.substring(with: match.range(at: 1))
                if let char = charStr.first, let sub = subscriptMap[char] {
                    text = (text as NSString).replacingCharacters(in: match.range, with: String(sub))
                }
            }
        }

        // Clean up remaining backslashes from common math keywords
        text = text.replacingOccurrences(of: "\\log", with: "log")
        text = text.replacingOccurrences(of: "\\ln", with: "ln")
        text = text.replacingOccurrences(of: "\\exp", with: "exp")
        text = text.replacingOccurrences(of: "\\min", with: "min")
        text = text.replacingOccurrences(of: "\\max", with: "max")
        text = text.replacingOccurrences(of: "\\lim", with: "lim")

        return text
    }

    /// Process inline math delimiters ($...$) inside standard text paragraphs.
    public static func processInlineMathDelimiters(_ text: String) -> String {
        // Regex for single dollar math expressions: $...$ (not preceded or followed by $)
        guard let regex = try? NSRegularExpression(pattern: #"(?<!\$)\$(?!\$)(.*?)(?<!\$)\$(?!\$)"#, options: []) else {
            return text
        }
        let nsString = text as NSString
        let matches = regex.matches(in: text, options: [], range: NSRange(location: 0, length: nsString.length)).reversed()
        var result = text
        for match in matches {
            let inner = nsString.substring(with: match.range(at: 1))
            let formatted = formatMathText(inner)
            result = (result as NSString).replacingCharacters(in: match.range, with: formatted)
        }
        return result
    }
}

// MARK: - Markdown Blocks Model

public struct OrderedListItem: Equatable, Sendable {
    public let number: Int
    public let text: String

    public init(number: Int, text: String) {
        self.number = number
        self.text = text
    }
}

public enum MarkdownBlock: Identifiable, Equatable, Sendable {
    case heading(level: Int, text: String)
    case paragraph(text: String)
    case codeBlock(language: String?, code: String)
    case bulletList(items: [String])
    case orderedList(items: [OrderedListItem])
    case table(headers: [String], rows: [[String]])
    case horizontalRule
    case blockquote(text: String)
    case mathBlock(math: String)

    public var id: String {
        switch self {
        case .heading(let l, let t): return "h-\(l)-\(t)"
        case .paragraph(let t): return "p-\(t.prefix(40))"
        case .codeBlock(let l, let c): return "code-\(l ?? "")-\(c.prefix(40))"
        case .bulletList(let i): return "bl-\(i.joined().prefix(40))"
        case .orderedList(let i): return "ol-\(i.map { "\($0.number).\($0.text)" }.joined().prefix(40))"
        case .table(let h, _): return "tbl-\(h.joined().prefix(40))"
        case .horizontalRule: return "hr-\(UUID().uuidString)"
        case .blockquote(let t): return "bq-\(t.prefix(40))"
        case .mathBlock(let m): return "mb-\(m.prefix(40))"
        }
    }
}

// MARK: - Markdown Parser

public struct MarkdownParser {
    /// Determines whether a line indicates the start of a recognized non-paragraph block.
    private static func isBlockStart(_ trimmed: String) -> Bool {
        if trimmed.hasPrefix("```") || trimmed.hasPrefix("$$") || trimmed.hasPrefix(">") {
            return true
        }
        if trimmed.hasPrefix("- ") || trimmed.hasPrefix("* ") || trimmed.hasPrefix("+ ") {
            return true
        }
        if trimmed == "---" || trimmed == "***" || trimmed == "___" ||
           (trimmed.count >= 3 && trimmed.allSatisfy({ $0 == "-" || $0 == "*" || $0 == "_" })) {
            return true
        }
        // Valid heading: 1-6 hashes followed by a space
        if trimmed.hasPrefix("#") {
            var level = 0
            while level < trimmed.count && trimmed[trimmed.index(trimmed.startIndex, offsetBy: level)] == "#" {
                level += 1
            }
            if level <= 6 && level < trimmed.count && trimmed[trimmed.index(trimmed.startIndex, offsetBy: level)] == " " {
                return true
            }
        }
        // Ordered list: e.g. "1. "
        if let firstSpace = trimmed.firstIndex(of: " "),
           let dotIdx = trimmed.firstIndex(of: "."),
           dotIdx < firstSpace,
           let _ = Int(trimmed[..<dotIdx]) {
            return true
        }
        // Table: starts and ends with '|'
        if trimmed.hasPrefix("|") && trimmed.hasSuffix("|") && trimmed.contains("|") {
            return true
        }
        return false
    }

    public static func parse(_ raw: String) -> [MarkdownBlock] {
        var blocks: [MarkdownBlock] = []
        let lines = raw.components(separatedBy: "\n")
        var idx = 0

        while idx < lines.count {
            let startIdx = idx
            let line = lines[idx]
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            // 1. Skip empty lines
            if trimmed.isEmpty {
                idx += 1
                continue
            }

            // 2. Fenced code block (```lang ... ```)
            if trimmed.hasPrefix("```") {
                let lang = String(trimmed.dropFirst(3)).trimmingCharacters(in: .whitespaces)
                var codeLines: [String] = []
                idx += 1
                while idx < lines.count {
                    let nextLine = lines[idx]
                    if nextLine.trimmingCharacters(in: .whitespaces).hasPrefix("```") {
                        idx += 1
                        break
                    }
                    codeLines.append(nextLine)
                    idx += 1
                }
                blocks.append(.codeBlock(language: lang.isEmpty ? nil : lang, code: codeLines.joined(separator: "\n")))
                continue
            }

            // 3. Block math ($$ ... $$)
            if trimmed.hasPrefix("$$") {
                var mathLines: [String] = []
                let rest = String(trimmed.dropFirst(2))
                if rest.hasSuffix("$$") && rest.count > 2 {
                    let content = String(rest.dropLast(2))
                    blocks.append(.mathBlock(math: MathFormatter.formatMathText(content.trimmingCharacters(in: .whitespaces))))
                    idx += 1
                    continue
                } else if !rest.isEmpty {
                    mathLines.append(rest)
                }
                idx += 1
                while idx < lines.count {
                    let nextLine = lines[idx]
                    let nextTrimmed = nextLine.trimmingCharacters(in: .whitespaces)
                    if nextTrimmed.hasSuffix("$$") {
                        let finalContent = String(nextTrimmed.dropLast(2))
                        if !finalContent.isEmpty {
                            mathLines.append(finalContent)
                        }
                        idx += 1
                        break
                    }
                    mathLines.append(nextLine)
                    idx += 1
                }
                blocks.append(.mathBlock(math: MathFormatter.formatMathText(mathLines.joined(separator: "\n").trimmingCharacters(in: .whitespaces))))
                continue
            }

            // 4. Horizontal rule (--- or *** or ___)
            if trimmed == "---" || trimmed == "***" || trimmed == "___" ||
               (trimmed.count >= 3 && trimmed.allSatisfy({ $0 == "-" || $0 == "*" || $0 == "_" })) {
                blocks.append(.horizontalRule)
                idx += 1
                continue
            }

            // 5. Headings (# Heading)
            if trimmed.hasPrefix("#") {
                var level = 0
                while level < trimmed.count && trimmed[trimmed.index(trimmed.startIndex, offsetBy: level)] == "#" {
                    level += 1
                }
                if level <= 6 && level < trimmed.count && trimmed[trimmed.index(trimmed.startIndex, offsetBy: level)] == " " {
                    let title = String(trimmed.dropFirst(level + 1)).trimmingCharacters(in: .whitespaces)
                    let formattedTitle = MathFormatter.processInlineMathDelimiters(title)
                    blocks.append(.heading(level: level, text: formattedTitle))
                    idx += 1
                    continue
                } else if level <= 6 && level == trimmed.count {
                    // Streaming partial heading prefix (e.g. "#", "##", "###")
                    // No space or title has arrived yet in the stream.
                    // Safely render as paragraph and advance index so the parser never loops.
                    blocks.append(.paragraph(text: trimmed))
                    idx += 1
                    continue
                }
                // If level > 6 (e.g. "#######") or no space (e.g. "#tag"),
                // fall through to paragraph parser.
            }

            // 6. Blockquotes (> quote)
            if trimmed.hasPrefix(">") {
                var bqLines: [String] = []
                while idx < lines.count && lines[idx].trimmingCharacters(in: .whitespaces).hasPrefix(">") {
                    let content = String(lines[idx].trimmingCharacters(in: .whitespaces).dropFirst(1)).trimmingCharacters(in: .whitespaces)
                    bqLines.append(content)
                    idx += 1
                }
                let bqText = MathFormatter.processInlineMathDelimiters(bqLines.joined(separator: "\n"))
                blocks.append(.blockquote(text: bqText))
                continue
            }

            // 7. Markdown Tables (| col1 | col2 |)
            if trimmed.hasPrefix("|") && trimmed.hasSuffix("|") && trimmed.contains("|") {
                var tableLines: [String] = []
                while idx < lines.count {
                    let current = lines[idx].trimmingCharacters(in: .whitespaces)
                    if current.hasPrefix("|") && current.hasSuffix("|") {
                        tableLines.append(current)
                        idx += 1
                    } else {
                        break
                    }
                }
                if tableLines.count >= 2 {
                    var headerLine = tableLines[0]
                    if headerLine.hasPrefix("|") { headerLine.removeFirst() }
                    if headerLine.hasSuffix("|") { headerLine.removeLast() }
                    let headers = headerLine.components(separatedBy: "|").map { $0.trimmingCharacters(in: .whitespaces) }
                    var rows: [[String]] = []
                    // Skip index 1 if it's the divider row |---|---|
                    let startRow = tableLines[1].contains("---") ? 2 : 1
                    for rIdx in startRow..<tableLines.count {
                        var rowLine = tableLines[rIdx]
                        if rowLine.hasPrefix("|") { rowLine.removeFirst() }
                        if rowLine.hasSuffix("|") { rowLine.removeLast() }
                        let cols = rowLine.components(separatedBy: "|").map { $0.trimmingCharacters(in: .whitespaces) }
                        if !cols.isEmpty {
                            rows.append(cols.map { MathFormatter.processInlineMathDelimiters($0) })
                        }
                    }
                    blocks.append(.table(headers: headers.map { MathFormatter.processInlineMathDelimiters($0) }, rows: rows))
                    continue
                } else if tableLines.count == 1 {
                    // Single pipe row (not a complete table): safely render as paragraph
                    blocks.append(.paragraph(text: MathFormatter.processInlineMathDelimiters(tableLines[0])))
                    continue
                }
            }

            // 8. Bullet lists (- item or * item)
            if trimmed.hasPrefix("- ") || trimmed.hasPrefix("* ") || trimmed.hasPrefix("+ ") {
                var items: [String] = []
                while idx < lines.count {
                    let rawLine = lines[idx]
                    let cur = rawLine.trimmingCharacters(in: .whitespaces)
                    if cur.hasPrefix("- ") || cur.hasPrefix("* ") || cur.hasPrefix("+ ") {
                        let item = String(cur.dropFirst(2)).trimmingCharacters(in: .whitespaces)
                        items.append(MathFormatter.processInlineMathDelimiters(item))
                        idx += 1
                    } else if !cur.isEmpty && (rawLine.hasPrefix("  ") || rawLine.hasPrefix("\t")) && !items.isEmpty {
                        // Multiline list item continuation (check untrimmed raw line for indentation)
                        let lastIdx = items.count - 1
                        items[lastIdx] += " " + MathFormatter.processInlineMathDelimiters(cur)
                        idx += 1
                    } else {
                        break
                    }
                }
                blocks.append(.bulletList(items: items))
                continue
            }

            // 9. Ordered lists (1. item)
            if let firstSpace = trimmed.firstIndex(of: " "),
               let dotIdx = trimmed.firstIndex(of: "."),
               dotIdx < firstSpace,
               let _ = Int(trimmed[..<dotIdx]) {
                var items: [OrderedListItem] = []
                while idx < lines.count {
                    let rawLine = lines[idx]
                    let cur = rawLine.trimmingCharacters(in: .whitespaces)
                    if let curSpace = cur.firstIndex(of: " "),
                       let curDot = cur.firstIndex(of: "."),
                       curDot < curSpace,
                       let itemNum = Int(cur[..<curDot]) {
                        let text = String(cur[cur.index(after: curSpace)...]).trimmingCharacters(in: .whitespaces)
                        items.append(OrderedListItem(number: itemNum, text: MathFormatter.processInlineMathDelimiters(text)))
                        idx += 1
                    } else if !cur.isEmpty && (rawLine.hasPrefix("  ") || rawLine.hasPrefix("\t")) && !items.isEmpty {
                        // Multiline list item continuation (check untrimmed raw line for indentation)
                        let lastIdx = items.count - 1
                        let updatedText = items[lastIdx].text + " " + MathFormatter.processInlineMathDelimiters(cur)
                        items[lastIdx] = OrderedListItem(number: items[lastIdx].number, text: updatedText)
                        idx += 1
                    } else {
                        break
                    }
                }
                blocks.append(.orderedList(items: items))
                continue
            }

            // 10. Regular Paragraph
            // Accumulate lines until an empty line or a distinct block delimiter is encountered.
            var paragraphLines: [String] = []
            while idx < lines.count {
                let cur = lines[idx]
                let curTrimmed = cur.trimmingCharacters(in: .whitespaces)

                if !paragraphLines.isEmpty {
                    if curTrimmed.isEmpty || Self.isBlockStart(curTrimmed) {
                        break
                    }
                } else {
                    if curTrimmed.isEmpty {
                        break
                    }
                }
                paragraphLines.append(curTrimmed)
                idx += 1
            }

            if !paragraphLines.isEmpty {
                let rawParagraph = paragraphLines.joined(separator: " ")
                let formatted = MathFormatter.processInlineMathDelimiters(rawParagraph)
                blocks.append(.paragraph(text: formatted))
            }

            // DEFENSIVE PROGRESS INVARIANT:
            // Every iteration of this parser loop MUST advance idx by at least 1.
            // If any malformed, unrecognized, or edge-case line was neither handled nor advanced,
            // defensively consume the line as a paragraph and advance idx to guarantee termination.
            if idx <= startIdx {
                let fallback = lines[startIdx].trimmingCharacters(in: .whitespaces)
                if !fallback.isEmpty {
                    blocks.append(.paragraph(text: MathFormatter.processInlineMathDelimiters(fallback)))
                }
                idx = startIdx + 1
            }
        }

        return blocks
    }
}

// MARK: - SwiftUI Views for Markdown Blocks

public struct CodeBlockView: View {
    public let language: String?
    public let code: String

    public var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Header bar
            HStack {
                Text((language ?? "code").uppercased())
                    .font(.system(size: 11, weight: .bold, design: .monospaced))
                    .foregroundColor(.secondary)
                Spacer()
                Button(action: copyToClipboard) {
                    HStack(spacing: 4) {
                        Image(systemName: "doc.on.doc")
                            .font(.system(size: 11))
                        Text("Copy")
                            .font(.system(size: 11, weight: .medium))
                    }
                    .foregroundColor(.secondary)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 3)
                    .background(Color.secondary.opacity(0.12))
                    .cornerRadius(4)
                }
                .buttonStyle(.plain)
                .help("Copy code to clipboard")
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(Color(NSColor.controlBackgroundColor).opacity(0.8))

            Divider()

            // Code content with horizontal scrolling
            ScrollView(.horizontal, showsIndicators: true) {
                Text(code)
                    .font(.system(size: 13, design: .monospaced))
                    .foregroundColor(Color(NSColor.textColor))
                    .padding(10)
                    .textSelection(.enabled)
            }
        }
        .background(Color(NSColor.textBackgroundColor))
        .cornerRadius(8)
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(Color.secondary.opacity(0.2), lineWidth: 1)
        )
        .padding(.vertical, 4)
    }

    private func copyToClipboard() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(code, forType: .string)
    }
}

public struct MarkdownTableView: View {
    public let headers: [String]
    public let rows: [[String]]

    public var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 0) {
                // Headers
                HStack(spacing: 0) {
                    ForEach(headers.indices, id: \.self) { idx in
                        Text(LocalizedStringKey(headers[idx]))
                            .font(.system(size: 12, weight: .bold))
                            .frame(minWidth: 80, alignment: .leading)
                            .padding(8)
                        if idx < headers.count - 1 {
                            Divider()
                        }
                    }
                }
                .background(Color.secondary.opacity(0.15))

                Divider()

                // Rows
                ForEach(rows.indices, id: \.self) { rIdx in
                    let row = rows[rIdx]
                    HStack(spacing: 0) {
                        ForEach(headers.indices, id: \.self) { cIdx in
                            let cellText = cIdx < row.count ? row[cIdx] : ""
                            Text(LocalizedStringKey(cellText))
                                .font(.system(size: 12))
                                .frame(minWidth: 80, alignment: .leading)
                                .padding(8)
                            if cIdx < headers.count - 1 {
                                Divider()
                            }
                        }
                    }
                    .background(rIdx % 2 == 1 ? Color.secondary.opacity(0.05) : Color.clear)
                    if rIdx < rows.count - 1 {
                        Divider()
                    }
                }
            }
            .cornerRadius(6)
            .overlay(
                RoundedRectangle(cornerRadius: 6)
                    .stroke(Color.secondary.opacity(0.2), lineWidth: 1)
            )
        }
        .padding(.vertical, 6)
        .textSelection(.enabled)
    }
}

public struct MarkdownMathBlockView: View {
    public let math: String

    public var body: some View {
        HStack {
            Spacer()
            Text(math)
                .font(.system(size: 14, weight: .medium, design: .serif))
                .italic()
                .padding(.horizontal, 16)
                .padding(.vertical, 8)
                .background(Color.secondary.opacity(0.08))
                .cornerRadius(6)
                .textSelection(.enabled)
            Spacer()
        }
        .padding(.vertical, 4)
    }
}

public struct MarkdownBlockRenderer: View {
    public let block: MarkdownBlock

    public var body: some View {
        switch block {
        case .heading(let level, let text):
            Text(LocalizedStringKey(text))
                .font(headingFont(level: level))
                .foregroundColor(headingColor(level: level))
                .padding(.top, level == 1 ? 8 : (level == 2 ? 6 : 4))
                .padding(.bottom, 2)
                .textSelection(.enabled)

        case .paragraph(let text):
            Text(LocalizedStringKey(text))
                .font(.system(size: 14))
                .lineSpacing(3)
                .textSelection(.enabled)

        case .codeBlock(let language, let code):
            CodeBlockView(language: language, code: code)

        case .bulletList(let items):
            VStack(alignment: .leading, spacing: 4) {
                ForEach(items.indices, id: \.self) { idx in
                    HStack(alignment: .top, spacing: 6) {
                        Text("•")
                            .font(.system(size: 14, weight: .bold))
                            .foregroundColor(.secondary)
                        Text(LocalizedStringKey(items[idx]))
                            .font(.system(size: 14))
                            .lineSpacing(2)
                            .textSelection(.enabled)
                    }
                }
            }
            .padding(.leading, 4)
            .padding(.vertical, 2)

        case .orderedList(let items):
            VStack(alignment: .leading, spacing: 4) {
                ForEach(items.indices, id: \.self) { idx in
                    HStack(alignment: .top, spacing: 6) {
                        Text("\(items[idx].number).")
                            .font(.system(size: 13, weight: .semibold, design: .monospaced))
                            .foregroundColor(.secondary)
                            .frame(minWidth: 20, alignment: .trailing)
                        Text(LocalizedStringKey(items[idx].text))
                            .font(.system(size: 14))
                            .lineSpacing(2)
                            .textSelection(.enabled)
                    }
                }
            }
            .padding(.leading, 4)
            .padding(.vertical, 2)

        case .table(let headers, let rows):
            MarkdownTableView(headers: headers, rows: rows)

        case .horizontalRule:
            Divider()
                .padding(.vertical, 6)

        case .blockquote(let text):
            HStack(spacing: 8) {
                Rectangle()
                    .fill(Color.accentColor.opacity(0.6))
                    .frame(width: 3)
                Text(LocalizedStringKey(text))
                    .font(.system(size: 13))
                    .italic()
                    .foregroundColor(.secondary)
                    .textSelection(.enabled)
            }
            .padding(.vertical, 4)

        case .mathBlock(let math):
            MarkdownMathBlockView(math: math)
        }
    }

    private func headingFont(level: Int) -> Font {
        switch level {
        case 1: return .system(size: 18, weight: .bold)
        case 2: return .system(size: 16, weight: .bold)
        case 3: return .system(size: 15, weight: .semibold)
        default: return .system(size: 14, weight: .semibold)
        }
    }

    private func headingColor(level: Int) -> Color {
        switch level {
        case 1: return .primary
        case 2: return .primary
        case 3: return .primary.opacity(0.9)
        default: return .secondary
        }
    }
}

// MARK: - Markdown View Coordinator & Rendering

@MainActor
public final class MarkdownCoordinator: ObservableObject {
    @Published public var blocks: [MarkdownBlock] = []
    private var lastParsedText: String = ""
    private var lastParseTime: Double = 0
    private var parseGeneration: Int = 0
    private var throttleTask: Task<Void, Never>? = nil

    public init(text: String) {
        self.lastParsedText = text
        self.lastParseTime = ProcessInfo.processInfo.systemUptime
        self.blocks = MarkdownParser.parse(text)
    }

    public func update(text: String, isStreaming: Bool) {
        guard text != lastParsedText else { return }

        parseGeneration += 1
        let thisGeneration = parseGeneration

        let now = ProcessInfo.processInfo.systemUptime
        let timeSinceLastParse = now - lastParseTime
        let throttleInterval = 0.08 // 80ms throttle (~12 parses/sec max, smooth 60fps UI)

        throttleTask?.cancel()

        if !isStreaming || blocks.isEmpty || timeSinceLastParse >= throttleInterval {
            lastParseTime = now
            lastParsedText = text

            if !isStreaming || text.count < 300 {
                let parsed = MarkdownParser.parse(text)
                if thisGeneration >= self.parseGeneration {
                    self.blocks = parsed
                }
            } else {
                throttleTask = Task.detached(priority: .userInitiated) {
                    let parsed = MarkdownParser.parse(text)
                    await MainActor.run {
                        if thisGeneration >= self.parseGeneration {
                            self.blocks = parsed
                        }
                    }
                }
            }
        } else {
            let remaining = throttleInterval - timeSinceLastParse
            throttleTask = Task {
                try? await Task.sleep(nanoseconds: UInt64(remaining * 1_000_000_000))
                if Task.isCancelled { return }

                let parsed = await Task.detached(priority: .userInitiated) {
                    return MarkdownParser.parse(text)
                }.value

                if !Task.isCancelled && thisGeneration >= self.parseGeneration {
                    self.blocks = parsed
                    self.lastParseTime = ProcessInfo.processInfo.systemUptime
                    self.lastParsedText = text
                }
            }
        }
    }
}

public struct MarkdownView: View {
    public let text: String
    public var isStreaming: Bool

    @StateObject private var coordinator: MarkdownCoordinator

    public init(text: String, isStreaming: Bool = false) {
        self.text = text
        self.isStreaming = isStreaming
        _coordinator = StateObject(wrappedValue: MarkdownCoordinator(text: text))
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(coordinator.blocks) { block in
                MarkdownBlockRenderer(block: block)
            }
        }
        .onChange(of: text) { _, newText in
            coordinator.update(text: newText, isStreaming: isStreaming)
        }
        .onChange(of: isStreaming) { _, streaming in
            if !streaming {
                coordinator.update(text: text, isStreaming: false)
            }
        }
    }
}
