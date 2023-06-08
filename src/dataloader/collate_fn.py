import torch


def static(data: list):
    padded_data = {
        key: torch.stack([torch.tensor(patient[key]) for patient in data])
        for key in data[0].keys()
    }

    return padded_data


def dynamic_padding(data: list):
    max_len = max([len(patient["concept"]) for patient in data])
    for patient in data:
        difference = max_len - len(patient["concept"])
        for key, values in patient.items():
            if key in ["age", "abspos"]:
                dtype = torch.float32
            else:
                dtype = torch.long

            if key == "target":
                if isinstance(values, float):
                    patient[key] = torch.tensor(values)
                    continue
                filler = torch.ones(difference, dtype=dtype) * -100
            else:
                filler = torch.zeros(difference, dtype=dtype)
            patient[key] = torch.cat((values.to(dtype), filler), dim=0)

    padded_data = {
        key: torch.stack([patient[key] for patient in data]) for key in data[0].keys()
    }

    return padded_data
