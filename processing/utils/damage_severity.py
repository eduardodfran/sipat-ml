def calculate_severity(bbox):
    """Calculate road damage severity based on bounding box area.

    Args:
        bbox: Normalized coordinates [xmin, ymin, xmax, ymax] (0.0 to 1.0).

    Returns:
        Severity string: "Minor", "Moderate", or "Severe".
    """
    xmin, ymin, xmax, ymax = bbox
    area_pct = round(((xmax - xmin) * (ymax - ymin)) * 100, 2)

    if area_pct < 2:
        return "Minor"
    elif area_pct <= 6:
        return "Moderate"
    else:
        return "Severe"


if __name__ == "__main__":
    test_boxes = [
        [0.1, 0.1, 0.15, 0.15],  # Small box (0.25%) -> Minor
        [0.2, 0.2, 0.3, 0.3],    # Medium box (1%) -> Minor
        [0.1, 0.1, 0.3, 0.2],    # 2% -> Moderate
        [0.1, 0.1, 0.3, 0.4],    # 6% -> Moderate
        [0.0, 0.0, 0.5, 0.5],    # 25% -> Severe
        [0.1, 0.1, 0.9, 0.9],    # 64% -> Severe
    ]

    for bbox in test_boxes:
        xmin, ymin, xmax, ymax = bbox
        area_pct = ((xmax - xmin) * (ymax - ymin)) * 100
        severity = calculate_severity(bbox)
        print(f"BBox: {bbox} | Area: {area_pct:.2f}% | Severity: {severity}")
