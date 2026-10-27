import SwiftUI

struct SIMCardMenu: View {
    @Environment(Store.self) private var store

    var body: some View {
        Menu {
            ForEach(store.cards) { card in
                Button(store.simAliases[card.id] ?? card.title, systemImage: store.selectedCard == card.id ? "checkmark.circle.fill" : "simcard") {
                    Task { await store.selectCard(card.id) }
                }
            }
        } label: {
            Label("切换 SIM 记录", systemImage: "simcard.2")
        }
        .labelStyle(.iconOnly)
        .disabled(store.cards.isEmpty)
    }
}
