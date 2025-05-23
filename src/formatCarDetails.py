import numpy as np
import pandas as pd
import logging
import argparse

# Formats ManteoMax's spreadsheet on car details into a more manageable format for this application
# Removes cars with empty ordinals, removes unimportant columns, etc

logging.basicConfig(level=logging.INFO)

def transform(input_filename: str, output_filename: str):
    input = pd.read_csv(input_filename, index_col=False)

    # Drop the first 2 columns
    input = input.drop(input.columns[[0, 1, ]], axis=1)

    # Rename the Makes and Models columns
    input.rename(columns={input.columns[3]: "Make", input.columns[4]: "Model"}, inplace=True)

    # Get the relevant columns
    input = input[["Nickname", "Ordinal", "Year",
                   "Make", "Model", "Car Division",
                   "Spec", "Region", "Country"]]
    
    # Remove entries where there is no Ordinal value
    input.dropna(subset="Ordinal", inplace=True)

    # Convert the Ordinal field to an integer type
    input["Ordinal"] = input["Ordinal"].apply(lambda x : x.replace(",", ""))
    input["Ordinal"] = input["Ordinal"].apply(pd.to_numeric)
    
    # Make Ordinal the index
    input.set_index("Ordinal", inplace=True)
    input.sort_index(inplace=True)

    # Save the dataframe
    input.to_csv(output_filename)
    print(input)

def main():
    cli_parser = argparse.ArgumentParser(
        description="Transforms ManteoMax's spreadsheet on car details into a more manageable format for the Forza-Analyse application"
    )

    cli_parser.add_argument('input_filename', type=str,
                            help='path to Manteo Max CSV spreadsheet')
    
    cli_parser.add_argument('output_filename', type=str,
                            help='path to save the output to')
    
    args = cli_parser.parse_args()

    transform(args.input_filename, args.output_filename)

if __name__ == "__main__":
    main()