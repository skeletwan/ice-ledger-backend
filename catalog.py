"""Hockey checklist: flagship sets + learned rows from saved cards."""
import re
import sqlite3
import time

SETS = [
    # year, brand, set, insert, parallels (name, print hint)
    ("2025-26", "Upper Deck", "Series 1", "Young Guns", ["Base", "Clear Cut", "Exclusives /100", "High Gloss /10", "Deluxe /250"]),
    ("2025-26", "Upper Deck", "Series 1", "", ["Base", "Canvas", "UD Exclusives /100", "High Gloss /10"]),
    ("2025-26", "Upper Deck", "Series 1", "Sizzle Reel", ["Base", "Speckle", "Red /199", "Gold /25", "Printing Plate 1/1"]),
    ("2025-26", "Upper Deck", "Series 2", "Sizzle Reel", ["Base", "Speckle", "Red /199", "Gold /25", "Printing Plate 1/1"]),
    ("2024-25", "Upper Deck", "Series 1", "Sizzle Reel", ["Base", "Speckle", "Red /199", "Gold /25"]),
    ("2024-25", "Upper Deck", "Series 2", "Sizzle Reel", ["Base", "Speckle", "Red /199", "Gold /25"]),
    ("2024-25", "Upper Deck", "Series 1", "Young Guns", ["Base", "Clear Cut", "Exclusives /100", "High Gloss /10", "Deluxe /250"]),
    ("2024-25", "Upper Deck", "Series 1", "", ["Base", "Canvas", "UD Exclusives /100", "High Gloss /10"]),
    ("2024-25", "Upper Deck", "Series 2", "Young Guns", ["Base", "Clear Cut", "Exclusives /100", "High Gloss /10", "Deluxe /250"]),
    ("2023-24", "Upper Deck", "Series 1", "Young Guns", ["Base", "Clear Cut", "Exclusives /100", "High Gloss /10", "Deluxe /250"]),
    ("2023-24", "Upper Deck", "Series 2", "Young Guns", ["Base", "Clear Cut", "Exclusives /100", "High Gloss /10"]),
    ("2022-23", "Upper Deck", "Series 1", "Young Guns", ["Base", "Clear Cut", "Exclusives /100", "High Gloss /10"]),
    ("2021-22", "Upper Deck", "Series 1", "Young Guns", ["Base", "Clear Cut", "Exclusives /100", "High Gloss /10"]),
    ("2020-21", "Upper Deck", "Series 1", "Young Guns", ["Base", "Clear Cut", "Exclusives /100", "High Gloss /10"]),
    ("2019-20", "Upper Deck", "Series 1", "Young Guns", ["Base", "Clear Cut", "Exclusives /100", "High Gloss /10"]),
    ("2016-17", "Upper Deck", "Series 1", "Young Guns", ["Base", "Exclusives /100", "High Gloss /10"]),
    ("2015-16", "Upper Deck", "Series 1", "Young Guns", ["Base", "Exclusives /100", "High Gloss /10"]),
    ("2005-06", "Upper Deck", "Series 1", "Young Guns", ["Base"]),
    ("2024-25", "Upper Deck", "Extended", "Young Guns", ["Base", "Clear Cut", "Exclusives /100"]),
    ("2025-26", "Upper Deck", "SP Authentic", "Holofoil", ["Base", "Red", "Gold /25"]),
    ("2024-25", "Upper Deck", "SP Authentic", "Holofoil", ["Base", "Red", "Gold /25"]),
    ("2023-24", "Upper Deck", "SP Authentic", "Holofoil", ["Base"]),
    ("2022-23", "Upper Deck", "SP Authentic", "Holofoil", ["Base"]),
    ("2021-22", "Upper Deck", "SP Authentic", "Holofoil", ["Base"]),
    ("2023-24", "Upper Deck", "SP Authentic", "Future Watch", ["Base /999", "Inscribed", "Gold /150"]),
    ("2024-25", "Upper Deck", "The Cup", "", ["Base /249", "Gold /24", "Black /8", "Printing Plate 1/1"]),
    ("2023-24", "Upper Deck", "The Cup", "", ["Base /249", "Gold /24", "Black /8"]),
    ("2024-25", "Upper Deck", "Artifacts", "", ["Base", "Ruby /499", "Sapphire /85", "Emerald /99", "Gold /65", "Auto"]),
    ("2024-25", "Upper Deck", "Synergy", "", ["Base", "Red", "Purple /899", "Gold /65", "Black /5"]),
    ("2024-25", "Upper Deck", "Ice", "", ["Base", "Green /10", "Black /5", "Premieres"]),
    ("2024-25", "Upper Deck", "Premier", "", ["Base /299", "Gold /25", "Black /5"]),
    ("2024-25", "Upper Deck", "Stature", "", ["Base /399", "Red /35", "Black /5"]),
    ("2024-25", "Upper Deck", "SPx", "", ["Base", "Finite /199", "Auto"]),
    ("2024-25", "O-Pee-Chee", "OPC", "", ["Base", "Red Border", "Rainbow", "Black /100"]),
    ("2023-24", "O-Pee-Chee", "OPC", "", ["Base", "Red Border", "Rainbow"]),
    ("2024-25", "O-Pee-Chee", "Platinum", "", ["Base", "Sunset", "Rainbow", "Golden Treasures 1/1"]),
    ("2024-25", "Parkhurst", "Parkhurst", "", ["Base", "Red", "Gold /10"]),
    ("1990-91", "Score", "Score", "", ["Base"]),
    ("1990-91", "Upper Deck", "Upper Deck", "", ["Base"]),
    ("1991-92", "Upper Deck", "Upper Deck", "", ["Base"]),
    ("1993-94", "Upper Deck", "SP", "", ["Base", "Die-Cut"]),
    ("1994-95", "Upper Deck", "SP", "", ["Base"]),
    ("1995-96", "Upper Deck", "SP", "", ["Base"]),
    ("1996-97", "Upper Deck", "Series 1", "", ["Base"]),
    ("1997-98", "Pinnacle", "Be A Player", "", ["Base", "Autograph"]),
    ("2003-04", "Upper Deck", "Series 1", "Young Guns", ["Base"]),
    ("2004-05", "Upper Deck", "Series 1", "", ["Base"]),
    ("2023-24", "Upper Deck", "Starquest", "", ["Base", "Red", "Green", "Blue", "Gold", "Purple", "Orange", "Black /5", "Superfractor 1/1"]),
    ("2024-25", "Upper Deck", "Starquest", "", ["Base", "Red", "Green", "Blue", "Gold", "Purple", "Orange", "Black /5"]),
    ("2022-23", "Upper Deck", "Starquest", "", ["Red", "Green", "Blue", "Gold", "Purple"]),
    ("2024-25", "Upper Deck", "Choice", "", ["Base", "Reserve", "Starquest Red", "Starquest Green"]),
    ("2023-24", "Upper Deck", "Choice", "", ["Base", "Reserve"]),
    ("2024-25", "Upper Deck", "Splendor", "", ["Base /8", "Gold /3", "Black 1/1"]),
    ("2023-24", "Upper Deck", "Splendor", "", ["Base /8", "Gold /3"]),
    ("2024-25", "Upper Deck", "Trilogy", "", ["Base", "Red /999", "Gold /49", "Rookie Premieres"]),
    ("2024-25", "Upper Deck", "Ultimate", "", ["Base /399", "Gold /25", "Black /5"]),
    ("2024-25", "Upper Deck", "Black Diamond", "", ["Base", "Gem /99", "Quad Jersey"]),
    ("2014-15", "Upper Deck", "Series 1", "Young Guns", ["Base"]),
    ("2018-19", "Upper Deck", "Series 1", "Young Guns", ["Base", "Exclusives /100"]),
    ("2007-08", "Upper Deck", "Series 1", "Young Guns", ["Base"]),
    ("2008-09", "Upper Deck", "Series 1", "Young Guns", ["Base"]),
    ("2009-10", "Upper Deck", "Series 1", "Young Guns", ["Base"]),
    ("2010-11", "Upper Deck", "Series 1", "Young Guns", ["Base"]),
    ("2011-12", "Upper Deck", "Series 1", "Young Guns", ["Base"]),
    ("2012-13", "Upper Deck", "Series 1", "Young Guns", ["Base"]),
    ("2013-14", "Upper Deck", "Series 1", "Young Guns", ["Base"]),
    ("1993-94", "Fleer", "Ultra", "", ["Base", "Ultra Power", "Scoring Kings"]),
    ("1994-95", "Fleer", "Ultra", "", ["Base"]),
    ("1992-93", "Skybox", "Impact", "", ["Base", "Rookie"]),
    ("2024-25", "Upper Deck", "MVP", "", ["Base", "Gold Script", "Super Script"]),
    ("2024-25", "Upper Deck", "Chronology", "", ["Base", "Gold /25"]),
    ("2024-25", "Upper Deck", "Credentials", "", ["Base /99", "Gold /10"]),
    ("2019-20", "Upper Deck", "Allure", "", ["Base", "Black Rainbow", "Red Rainbow", "Orange Slice", "Glitter Bomb", "Gold Glitter Bomb /199", "Green Rainbow /99", "Blue Line /35", "Purple Diamond /10", "Golden Treasures 1/1"]),
    ("2020-21", "Upper Deck", "Allure", "", ["Base", "Black Rainbow", "Red Rainbow", "Orange Slice", "Glitter Bomb", "Gold Glitter Bomb /199", "Purple Diamond /10", "Golden Treasures 1/1"]),
    ("2021-22", "Upper Deck", "Allure", "", ["Base", "Black Rainbow", "Red Rainbow", "Orange Slice", "Glitter Bomb", "Gold Glitter Bomb /199", "Green Rainbow /99", "Blue Line /35", "Purple Diamond /10", "Golden Treasures 1/1"]),
    ("2022-23", "Upper Deck", "Allure", "", ["Base", "Black Rainbow", "Red Rainbow", "Orange Slice", "Pink Lemonade", "Glitter Bomb", "Gold Glitter Bomb /199", "Green Rainbow /99", "Blue Line /35", "Purple Diamond /10", "Golden Treasures 1/1"]),
    ("2023-24", "Upper Deck", "Allure", "", ["Base", "Black Rainbow", "Red Rainbow", "Orange Slice", "Pink Lemonade", "Yellow Taxi", "Confetti", "Hypnosis", "Gold Glitter Bomb /199", "Green Rainbow /99", "Blue Line /35", "Purple Diamond /10", "Golden Treasures 1/1"]),
    ("2024-25", "Upper Deck", "Allure", "", ["Base", "Black Rainbow", "Red Rainbow", "Orange Slice", "Glitter Bomb", "Flying Puck", "Gold Glitter Bomb /199", "Green Rainbow /99", "Blue Line /35", "Purple Diamond /10", "Golden Treasures 1/1"]),
    ("2025-26", "Upper Deck", "Allure", "", ["Base", "Black Rainbow", "Red Rainbow", "Orange Slice", "Glitter Bomb", "Gold Glitter Bomb /199", "Green Quartz /99", "Blue Line /35", "Purple Diamond /10", "Golden Treasures 1/1"]),
    ("2023-24", "Upper Deck", "Allure", "Color Flow", ["Red-Orange", "Orange-Yellow", "Yellow-Green", "Green-Blue", "Blue-Purple", "Golden Treasures 1/1"]),
    ("2024-25", "Upper Deck", "Allure", "Color Flow", ["Red-Orange", "Orange-Yellow", "Yellow-Green", "Green-Blue", "Blue-Purple"]),
    ("2025-26", "Upper Deck", "Allure", "Color Flow", ["Red-Orange", "Orange-Yellow", "Yellow-Green", "Green-Blue", "Blue-Purple"]),
    ("2024-25", "Upper Deck", "Allure", "Rookie", ["Base", "Black Rainbow", "Red Rainbow", "Orange Slice", "Purple Diamond /10"]),
    ("2025-26", "Upper Deck", "Allure", "Rookie", ["Base", "Black Rainbow", "Red Rainbow", "Orange Slice", "Purple Diamond /10"]),
]


def _season(start):
    return f"{start}-{str(start+1)[2:]}"


_YG = ["Base", "Clear Cut", "Exclusives /100", "High Gloss /10"]
_YG_DEL = ["Base", "Clear Cut", "Exclusives /100", "High Gloss /10", "Deluxe /250"]
for _y in range(2005, 2026):
    SETS.append((_season(_y), "Upper Deck", "Series 1", "Young Guns", list(_YG_DEL if _y >= 2015 else ["Base"])))
    SETS.append((_season(_y), "Upper Deck", "Series 2", "Young Guns", list(_YG if _y >= 2015 else ["Base"])))
    SETS.append((_season(_y), "Upper Deck", "Series 1", "", ["Base", "Canvas"]))
    SETS.append((_season(_y), "Upper Deck", "Series 2", "", ["Base", "Canvas"]))
    SETS.append((_season(_y), "Upper Deck", "Extended", "Young Guns", ["Base", "Clear Cut", "Exclusives /100"]))
    SETS.append((_season(_y), "O-Pee-Chee", "OPC", "", ["Base", "Red Border", "Rainbow", "Black /100"]))
    SETS.append((_season(_y), "Upper Deck", "MVP", "", ["Base", "Gold Script", "Super Script"]))

for _y in range(2015, 2026):
    SETS.append((_season(_y), "Upper Deck", "SP Authentic", "Future Watch", ["Base /999", "Inscribed", "Gold /150", "Patch Autograph"]))
    SETS.append((_season(_y), "Upper Deck", "The Cup", "", ["Base /249", "Gold /24", "Black /8", "Printing Plate 1/1", "Rookie Auto Patch"]))
    SETS.append((_season(_y), "Upper Deck", "Artifacts", "", ["Base", "Ruby /499", "Emerald /99", "Sapphire /85", "Gold /65", "Auto"]))
    SETS.append((_season(_y), "Upper Deck", "Ice", "", ["Base", "Green /10", "Black /5", "Premieres /99"]))
    SETS.append((_season(_y), "Upper Deck", "Premier", "", ["Base /299", "Gold /25", "Black /5", "Rookie Auto Patch"]))
    SETS.append((_season(_y), "Upper Deck", "Stature", "", ["Base /399", "Red /35", "Black /5"]))
    SETS.append((_season(_y), "Upper Deck", "SPx", "", ["Base", "Finite /199", "Auto"]))
    SETS.append((_season(_y), "Upper Deck", "Synergy", "", ["Base", "Red", "Purple /899", "Gold /65", "Black /5"]))
    SETS.append((_season(_y), "Upper Deck", "Trilogy", "", ["Base", "Red /999", "Gold /49", "Rookie Premieres"]))
    SETS.append((_season(_y), "Upper Deck", "Ultimate Collection", "", ["Base /399", "Gold /25", "Black /5", "Rookie Auto Patch"]))
    SETS.append((_season(_y), "Upper Deck", "Black Diamond", "", ["Base", "Gem /99", "Quad Jersey", "Diamond Relics"]))
    SETS.append((_season(_y), "O-Pee-Chee", "Platinum", "", ["Base", "Sunset", "Rainbow", "Golden Treasures 1/1", "Matte"]))
    SETS.append((_season(_y), "Parkhurst", "Parkhurst", "", ["Base", "Red", "Gold /10", "Champions"]))
    SETS.append((_season(_y), "Upper Deck", "Clear Cut", "", ["Base", "Gold /10", "Auto"]))
    SETS.append((_season(_y), "Upper Deck", "Portraits", "", ["Base", "Gold", "Auto"]))
    SETS.append((_season(_y), "Upper Deck", "Credentials", "", ["Base /99", "Gold /10"]))
    SETS.append((_season(_y), "Upper Deck", "Chronology", "", ["Base", "Gold /25"]))

SETS += [
    ("1990-91", "Upper Deck", "Upper Deck", "", ["Base"]),
    ("1991-92", "Upper Deck", "Upper Deck", "", ["Base"]),
    ("1992-93", "Upper Deck", "Upper Deck", "", ["Base"]),
    ("1993-94", "Upper Deck", "SP", "", ["Base", "Die-Cut", "Holoview"]),
    ("1994-95", "Upper Deck", "SP", "", ["Base", "Die-Cut"]),
    ("1995-96", "Upper Deck", "SP", "", ["Base"]),
    ("1996-97", "Upper Deck", "Series 1", "", ["Base"]),
    ("1997-98", "Upper Deck", "Series 1", "", ["Base"]),
    ("1998-99", "Upper Deck", "Series 1", "", ["Base"]),
    ("1999-00", "Upper Deck", "Series 1", "", ["Base"]),
    ("2000-01", "Upper Deck", "Series 1", "", ["Base"]),
    ("2001-02", "Upper Deck", "Series 1", "", ["Base"]),
    ("2002-03", "Upper Deck", "Series 1", "", ["Base"]),
    ("2004-05", "Upper Deck", "Series 1", "", ["Base"]),
    ("1997-98", "Pinnacle", "Be A Player", "", ["Base", "Autograph"]),
    ("1998-99", "Be A Player", "BAP Memorabilia", "", ["Base", "Autograph"]),
    ("1999-00", "Be A Player", "BAP Millennium", "", ["Base", "Autograph"]),
    ("2000-01", "Be A Player", "BAP Signature", "", ["Base", "Autograph"]),
    ("2001-02", "Be A Player", "BAP Signature", "", ["Base", "Autograph"]),
    ("2002-03", "Be A Player", "BAP Signature Series", "", ["Base", "Autograph"]),
    ("2005-06", "Upper Deck", "The Cup", "", ["Base /249", "Rookie Auto Patch"]),
    ("2006-07", "Upper Deck", "The Cup", "", ["Base /249", "Rookie Auto Patch"]),
    ("2007-08", "Upper Deck", "The Cup", "", ["Base /249", "Rookie Auto Patch"]),
    ("2008-09", "Upper Deck", "The Cup", "", ["Base /249", "Rookie Auto Patch"]),
    ("1990-91", "Score", "Score", "", ["Base"]),
    ("1991-92", "Score", "Score", "", ["Base", "Young Superstars"]),
    ("1992-93", "Score", "Score", "", ["Base"]),
    ("1990-91", "Pro Set", "Pro Set", "", ["Base"]),
    ("1991-92", "Pro Set", "Pro Set", "", ["Base"]),
    ("1991-92", "Parkhurst", "Parkhurst", "", ["Base"]),
    ("1992-93", "Parkhurst", "Parkhurst", "", ["Base"]),
    ("1993-94", "Parkhurst", "Parkhurst", "", ["Base"]),
    ("1994-95", "Parkhurst", "Parkhurst", "", ["Base"]),
    ("1990-91", "O-Pee-Chee", "OPC", "", ["Base", "Premier"]),
    ("1991-92", "O-Pee-Chee", "OPC", "", ["Base", "Premier"]),
    ("1992-93", "O-Pee-Chee", "OPC", "", ["Base"]),
    ("2007-08", "O-Pee-Chee", "OPC", "", ["Base"]),
    ("1993-94", "Fleer", "Ultra", "", ["Base", "Ultra Power", "Scoring Kings"]),
    ("1994-95", "Fleer", "Ultra", "", ["Base", "Ultra Power"]),
    ("1995-96", "Fleer", "Ultra", "", ["Base"]),
    ("1997-98", "Fleer", "Ultra", "", ["Base"]),
    ("1995-96", "Fleer", "Metal Universe", "", ["Base", "Precious Metal Gems"]),
    ("1996-97", "Fleer", "Metal Universe", "", ["Base"]),
    ("1997-98", "Fleer", "Metal Universe", "", ["Base"]),
    ("1996-97", "Skybox", "E-X2000", "", ["Base", "Credentials"]),
    ("1997-98", "Skybox", "E-X2001", "", ["Base"]),
    ("1992-93", "Skybox", "Impact", "", ["Base", "Rookie"]),
    ("1993-94", "Pinnacle", "Pinnacle", "", ["Base"]),
    ("1994-95", "Pinnacle", "Pinnacle", "", ["Base"]),
    ("1995-96", "Pinnacle", "Pinnacle", "", ["Base"]),
    ("1996-97", "Pinnacle", "Pinnacle", "", ["Base"]),
    ("1997-98", "Pinnacle", "Pinnacle", "", ["Base"]),
    ("1998-99", "Pacific", "Omega", "", ["Base"]),
    ("1999-00", "Pacific", "Paramount", "", ["Base"]),
    ("2000-01", "Pacific", "Crown Royale", "", ["Base"]),
    ("2001-02", "Pacific", "Crown Royale", "", ["Base", "Jerseys"]),
    ("2002-03", "Pacific", "Private Stock", "", ["Base"]),
    ("2003-04", "Pacific", "Atomic", "", ["Base"]),
    ("1995-96", "Donruss", "Donruss", "", ["Base"]),
    ("1996-97", "Donruss", "Donruss", "", ["Base"]),
    ("1997-98", "Donruss", "Donruss", "", ["Base"]),
    ("1998-99", "Donruss", "Priority", "", ["Base"]),
    ("2005-06", "ITG", "Heroes and Prospects", "", ["Base", "Autograph"]),
    ("2006-07", "ITG", "Heroes and Prospects", "", ["Base", "Autograph"]),
    ("2007-08", "ITG", "O Canada", "", ["Base"]),
    ("2013-14", "Panini", "Prizm", "", ["Base", "Silver", "Gold /10"]),
    ("2013-14", "Panini", "Select", "", ["Base", "Prizm"]),
    ("2013-14", "Panini", "National Treasures", "", ["Base /99", "Rookie Patch Auto"]),
    ("2013-14", "Panini", "Dominion", "", ["Base /99"]),
    ("2006-07", "Upper Deck", "SP Game Used", "", ["Base", "Authentic Fabrics", "SIGnificance"]),
    ("2007-08", "Upper Deck", "SP Game Used", "", ["Base", "Authentic Fabrics"]),
    ("2008-09", "Upper Deck", "SP Game Used", "", ["Base", "Authentic Fabrics"]),
    ("2009-10", "Upper Deck", "SP Game Used", "", ["Base", "Authentic Fabrics"]),
    ("2010-11", "Upper Deck", "SP Game Used", "", ["Base", "Authentic Fabrics"]),
    ("2005-06", "Upper Deck", "Ice", "", ["Base", "Premieres"]),
    ("2006-07", "Upper Deck", "Ice", "", ["Base", "Premieres"]),
    ("1999-00", "Upper Deck", "Wayne Gretzky Hockey", "", ["Base"]),
    ("2014-15", "Upper Deck", "Tim Hortons", "", ["Base", "Gold Etchings", "Platinum Profiles"]),
    ("2015-16", "Upper Deck", "Tim Hortons", "", ["Base", "Gold Etchings"]),
    ("2016-17", "Upper Deck", "Tim Hortons", "", ["Base"]),
    ("2017-18", "Upper Deck", "Tim Hortons", "", ["Base"]),
    ("2018-19", "Upper Deck", "Tim Hortons", "", ["Base"]),
    ("2019-20", "Upper Deck", "Tim Hortons", "", ["Base"]),
    ("2020-21", "Upper Deck", "Tim Hortons", "", ["Base"]),
    ("2021-22", "Upper Deck", "Tim Hortons", "", ["Base"]),
    ("2022-23", "Upper Deck", "Tim Hortons", "", ["Base"]),
    ("2023-24", "Upper Deck", "Tim Hortons", "", ["Base"]),
    ("2024-25", "Upper Deck", "Tim Hortons", "", ["Base"]),
    ("2025-26", "Upper Deck", "Tim Hortons", "", ["Base"]),
    ("2014-15", "Upper Deck", "World Junior Championship", "", ["Base", "Gold"]),
    ("2015-16", "Upper Deck", "World Junior Championship", "", ["Base"]),
    ("2016-17", "Upper Deck", "Team Canada Juniors", "", ["Base"]),
    ("2023-24", "Upper Deck", "Team Canada Juniors", "", ["Base", "Gold"]),
    ("2024-25", "Upper Deck", "Team Canada Juniors", "", ["Base", "Gold"]),
    ("2005-06", "McDonald's", "McDonald's", "", ["Base"]),
    ("2013-14", "Upper Deck", "Victory", "", ["Base"]),
    ("2008-09", "Upper Deck", "Collector's Choice", "", ["Base"]),
    ("2009-10", "Upper Deck", "Collector's Choice", "", ["Base"]),
    ("2017-18", "Upper Deck", "Engrained", "", ["Base", "Wood"]),
    ("2018-19", "Upper Deck", "Engrained", "", ["Base"]),
    ("2019-20", "Upper Deck", "Stature", "", ["Base /399"]),
    ("2020-21", "Upper Deck", "Stature", "", ["Base /399"]),
    ("2018-19", "Upper Deck", "Clear Cut", "", ["Base"]),
    ("2019-20", "Upper Deck", "Clear Cut", "", ["Base"]),
    ("2020-21", "Upper Deck", "Clear Cut", "", ["Base"]),
    ("2021-22", "Upper Deck", "Metal Universe", "", ["Base", "Precious Metal Gems /100", "Skybox Premium"]),
    ("2022-23", "Upper Deck", "Metal Universe", "", ["Base", "Precious Metal Gems /100"]),
    ("2023-24", "Upper Deck", "Metal Universe", "", ["Base", "Precious Metal Gems /100", "Skybox Premium"]),
    ("2024-25", "Upper Deck", "Metal Universe", "", ["Base", "Precious Metal Gems /100"]),
    ("2025-26", "Upper Deck", "Metal Universe", "", ["Base", "Precious Metal Gems /100"]),
    ("2021-22", "Upper Deck", "Skybox Metal Universe", "", ["Base", "Precious Metal Gems"]),
    ("2018-19", "Upper Deck", "Trilogy", "", ["Base", "Rookie Premieres"]),
    ("2019-20", "Upper Deck", "Trilogy", "", ["Base", "Rookie Premieres"]),
    ("2020-21", "Upper Deck", "Trilogy", "", ["Base", "Rookie Premieres"]),
    ("2015-16", "Upper Deck", "Black Diamond", "", ["Base", "Gem"]),
    ("2016-17", "Upper Deck", "Black Diamond", "", ["Base", "Gem"]),
    ("2017-18", "Upper Deck", "Black Diamond", "", ["Base", "Gem"]),
    ("2018-19", "Upper Deck", "Black Diamond", "", ["Base", "Gem"]),
    ("2010-11", "Upper Deck", "SP Authentic", "Future Watch", ["Base /999", "Autograph"]),
    ("2011-12", "Upper Deck", "SP Authentic", "Future Watch", ["Base /999"]),
    ("2012-13", "Upper Deck", "SP Authentic", "Future Watch", ["Base /999"]),
    ("2013-14", "Upper Deck", "SP Authentic", "Future Watch", ["Base /999"]),
    ("2014-15", "Upper Deck", "SP Authentic", "Future Watch", ["Base /999"]),
    ("2005-06", "Upper Deck", "SP Authentic", "Future Watch", ["Base /999"]),
    ("2007-08", "Upper Deck", "SP Authentic", "Future Watch", ["Base /999"]),
    ("2008-09", "Upper Deck", "SP Authentic", "Future Watch", ["Base /999"]),
    ("2009-10", "Upper Deck", "SP Authentic", "Future Watch", ["Base /999"]),
    ("2023-24", "Upper Deck", "Honorary", "", ["Base"]),
    ("2024-25", "Upper Deck", "Dazzlers", "", ["Blue", "Pink", "Orange", "Green"]),
    ("2023-24", "Upper Deck", "Dazzlers", "", ["Blue", "Pink", "Orange"]),
    ("2022-23", "Upper Deck", "Dazzlers", "", ["Blue", "Pink"]),
    ("2021-22", "Upper Deck", "Dazzlers", "", ["Blue", "Pink"]),
    ("2020-21", "Upper Deck", "Dazzlers", "", ["Blue", "Pink"]),
    ("2019-20", "Upper Deck", "Dazzlers", "", ["Blue", "Pink"]),
    ("2018-19", "Upper Deck", "Dazzlers", "", ["Blue", "Pink"]),
    ("2016-17", "Upper Deck", "Dazzlers", "", ["Blue", "Pink"]),
    ("2015-16", "Upper Deck", "Dazzlers", "", ["Blue", "Pink"]),
    ("2023-24", "Upper Deck", "UD Portraits", "", ["Base", "Gold"]),
    ("2024-25", "Upper Deck", "UD Portraits", "", ["Base", "Gold"]),
    ("2024-25", "Upper Deck", "Full Force", "", ["Base"]),
    ("2023-24", "Upper Deck", "Full Force", "", ["Base"]),
    ("2022-23", "Upper Deck", "Full Force", "", ["Base"]),
    ("2021-22", "Upper Deck", "Extended Series", "Young Guns", ["Base", "Exclusives /100"]),
    ("2020-21", "Upper Deck", "Extended Series", "Young Guns", ["Base"]),
    ("2019-20", "Upper Deck", "Extended Series", "Young Guns", ["Base"]),
    ("2018-19", "Upper Deck", "Extended Series", "Young Guns", ["Base"]),
    ("2024-25", "Leaf", "In The Game", "", ["Base", "Auto"]),
    ("2023-24", "Leaf", "In The Game", "", ["Base", "Auto"]),
    ("2024-25", "Upper Deck", "AHL", "", ["Base"]),
    ("2023-24", "Upper Deck", "AHL", "", ["Base"]),
    ("2022-23", "Upper Deck", "AHL", "", ["Base"]),
    ("2024-25", "Upper Deck", "CHL", "", ["Base"]),
    ("2023-24", "Upper Deck", "CHL", "", ["Base"]),
]



def catalog_parallel_terms(year="", set_name="", insert=""):
    """Parallel names from the seed checklist for this set (Allure, YG, etc.)."""
    y = str(year or "")[:4]
    sl = (set_name or "").lower()
    il = (insert or "").lower()
    generic = {"red", "gold", "black", "green", "blue", "purple", "orange", "pink", "silver", "base", "auto", "patch"}
    out = []
    seen = set()
    for sy, brand, st, ins, pars in SETS:
        hay = f"{brand} {st} {ins}".lower()
        row_ins = (ins or "").lower()
        if sl:
            if st.lower() in sl or sl in st.lower():
                pass
            elif ins and ins.lower() in sl:
                pass
            else:
                continue
        elif il:
            if il not in hay and row_ins not in il:
                continue
        if il:
            if row_ins and row_ins not in il and il not in row_ins:
                continue
        else:
            if row_ins:
                continue
        if y and sy[:4] != y and str(year or "") != sy:
            if not sl or (st.lower() not in sl and sl not in hay):
                continue
        for p in pars:
            name = re.sub(r"\s*/\s*.+$", "", p).strip()
            key = name.lower()
            if not name or key in seen:
                continue
            if key in generic and "starquest" not in sl and "sizzle" not in sl and "sizzle" not in il:
                continue
            seen.add(key)
            out.append(name)
    return out


def _tok(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def ensure_catalog(con: sqlite3.Connection):
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          year TEXT,
          brand TEXT,
          set_name TEXT,
          insert_name TEXT,
          parallel TEXT,
          player TEXT,
          number TEXT,
          team TEXT,
          print_run TEXT,
          source TEXT,
          created TEXT
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS cat_player ON catalog(player)")
    con.execute("CREATE INDEX IF NOT EXISTS cat_set ON catalog(set_name)")
    con.execute("CREATE INDEX IF NOT EXISTS cat_year ON catalog(year)")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_votes (
          user_id INTEGER NOT NULL,
          fp TEXT NOT NULL,
          year TEXT,
          set_name TEXT,
          insert_name TEXT,
          parallel TEXT,
          player TEXT,
          number TEXT,
          team TEXT,
          created TEXT,
          PRIMARY KEY (user_id, fp)
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS cat_votes_fp ON catalog_votes(fp)")
    n = con.execute("SELECT COUNT(*) AS n FROM catalog WHERE source='seed'").fetchone()
    count = n["n"] if n else 0
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for year, brand, set_name, insert, pars in SETS:
        for p in pars:
            run = ""
            m = re.search(r"/\s*(\d+)|1/1", p)
            if m:
                run = m.group(0).replace(" ", "")
            rows.append((year, brand, set_name, insert or "", p, "", "", "", run, "seed", now))
    if not count:
        con.executemany(
            "INSERT INTO catalog(year,brand,set_name,insert_name,parallel,player,number,team,print_run,source,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        return
    for row in rows:
        hit = con.execute(
            "SELECT id FROM catalog WHERE year=? AND set_name=? AND insert_name=? AND parallel=? AND source='seed' LIMIT 1",
            (row[0], row[2], row[3], row[4]),
        ).fetchone()
        if not hit:
            con.execute(
                "INSERT INTO catalog(year,brand,set_name,insert_name,parallel,player,number,team,print_run,source,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )


def _fp(player, year, set_name, number, par, ins):
    return "|".join([
        _tok(player),
        _tok(year),
        _tok(set_name),
        _tok(number),
        _tok(par) or "base",
        _tok(ins),
    ])


def vote_card(con: sqlite3.Connection, user_id: int, card: dict):
    """One vote per user per fingerprint. Two distinct users promote it to catalog."""
    if not user_id:
        return
    player = (card.get("player") or "").strip()
    year = (card.get("year") or "").strip()
    set_name = (card.get("set") or "").strip()
    if len(player) < 3 or len(set_name) < 2:
        return
    if not re.search(r"[a-zA-Z]", player):
        return
    number = (card.get("number") or "").strip()
    par = (card.get("parallel") or "").strip() or "Base"
    ins = (card.get("insert") or "").strip()
    team = (card.get("team") or "").strip()
    fp = _fp(player, year, set_name, number, par, ins)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    con.execute(
        """INSERT OR REPLACE INTO catalog_votes(user_id,fp,year,set_name,insert_name,parallel,player,number,team,created)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (user_id, fp, year, set_name, ins, par, player, number, team, now),
    )
    n = con.execute("SELECT COUNT(*) AS n FROM catalog_votes WHERE fp=?", (fp,)).fetchone()["n"]
    if n < 2:
        return
    hit = con.execute(
        """SELECT id FROM catalog WHERE lower(ifnull(player,''))=? AND year=? AND lower(set_name)=?
           AND ifnull(number,'')=? AND lower(ifnull(parallel,''))=? LIMIT 1""",
        (player.lower(), year, set_name.lower(), number, par.lower()),
    ).fetchone()
    if hit:
        return
    con.execute(
        "INSERT INTO catalog(year,brand,set_name,insert_name,parallel,player,number,team,print_run,source,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (year, "", set_name, ins, par, player, number, team, "", "voted", now),
    )


def learn_card(con: sqlite3.Connection, card: dict):
    return


def catalog_matches(con: sqlite3.Connection, data: dict, limit: int = 3):
    player = _tok(data.get("player"))
    year = str(data.get("year") or "").strip()
    st = _tok(data.get("set"))
    num = str(data.get("number") or "").strip()
    par = _tok(data.get("parallel"))
    ins = _tok(data.get("insert"))
    if not player and not st:
        return []
    rows = con.execute(
        """SELECT year, brand, set_name, insert_name, parallel, player, number, team, print_run, source
           FROM catalog
           WHERE (?='' OR player='' OR lower(player) LIKE ?)
             AND (?='' OR year='' OR year=?)
             AND (?='' OR lower(set_name) LIKE ? OR lower(insert_name) LIKE ? OR lower(parallel) LIKE ?)
           LIMIT 500""",
        (
            player, f"%{player}%" if player else "%",
            year, year,
            st, f"%{st}%" if st else "%", f"%{st}%" if st else "%", f"%{st}%" if st else "%",
        ),
    ).fetchall()
    scored = []
    seen = set()
    for r in rows:
        set_t = _tok(r["set_name"])
        par_t = _tok(r["parallel"])
        ins_t = _tok(r["insert_name"])
        pl_t = _tok(r["player"])
        score = 0
        if player and player in pl_t:
            score += 8
        if year and r["year"] == year:
            score += 4
        if st and (st in set_t or set_t in st):
            score += 5
        if num and str(r["number"] or "") == num:
            score += 4
        if par and (par in par_t or par_t in par):
            score += 4
        if ins and (ins in ins_t or ins_t in ins):
            score += 3
        if "young gun" in ins or "young gun" in st:
            if "young gun" in ins_t:
                score += 2
        if score < 5:
            continue
        key = (r["year"], r["set_name"], r["insert_name"], r["parallel"], r["player"], r["number"])
        if key in seen:
            continue
        seen.add(key)
        label_bits = [r["year"], r["set_name"], r["insert_name"] or None, r["parallel"] if r["parallel"] not in ("", "Base") else None]
        if r["player"]:
            label_bits = [r["player"]] + label_bits
        if r["number"]:
            label_bits.append("#" + r["number"])
        scored.append(
            (
                score,
                {
                    "year": r["year"],
                    "set": r["set_name"],
                    "insert": r["insert_name"] or "",
                    "parallel": r["parallel"] or "",
                    "player": r["player"] or data.get("player") or "",
                    "number": r["number"] or data.get("number") or "",
                    "team": r["team"] or data.get("team") or "",
                    "label": " · ".join(x for x in label_bits if x),
                    "source": r["source"],
                    "score": score,
                },
            )
        )
    scored.sort(key=lambda x: -x[0])
    return [x[1] for x in scored[:limit]]


def catalog_stats(con: sqlite3.Connection):
    tot = con.execute("SELECT COUNT(*) AS n FROM catalog").fetchone()["n"]
    seed = con.execute("SELECT COUNT(*) AS n FROM catalog WHERE source='seed'").fetchone()["n"]
    voted = con.execute("SELECT COUNT(*) AS n FROM catalog WHERE source='voted'").fetchone()["n"]
    pending = con.execute("SELECT COUNT(DISTINCT fp) AS n FROM catalog_votes").fetchone()["n"]
    return {"rows": tot, "seed": seed, "voted": voted, "pending": pending}
