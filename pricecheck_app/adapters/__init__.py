from .mediamarkt import MediaMarkt

# Each adapter owns fetching and normalization for its retailer.
ADAPTERS = {"mediamarkt": MediaMarkt}
