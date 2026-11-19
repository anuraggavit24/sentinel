Test-only PKI (never used outside the gateway's unit tests and the dev fake peer).
Each organisation: a CA (ca.crt/ca.key), a client (user.crt/user.key, signs transactions) and a peer
(peer.crt/peer.key, signs endorsements), all EC P-256 like Fabric's. `rogue/` is a self-signed
certificate with BankB's peer name that no BankB CA issued: the tests use it to show that a valid
signature from an unknown key is still reported as not issued by the bank.
`regulator/` was added for the multi-organisation dev network. These keys are public on purpose: they sign
nothing outside tests.
