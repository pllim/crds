"""This module defines replacement functionality for the CDBS "certify" program
used to check parameter values in .fits reference files.   It verifies that FITS
files define required parameters and that they have legal values.
"""
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import

# ============================================================================

import os
from collections import defaultdict
import gc
import re

import numpy as np

# ============================================================================

import crds

from crds.core import pysh, log, config, utils, rmap, cmdline
from crds.core.exceptions import InvalidFormatError, TypeSetupError, ValidationError

from crds import data_file, diff, tables
from crds.client import api
from crds.io import abstract

from . import mapping_parser
from . import validators
from . import reftypes

# ============================================================================

class Certifier(object):
    """Baseclass for all certifiers: references, mappings, etc."""

    def __init__(self, filename, context=None, check_references=False, 
                 compare_old_reference=False, dump_provenance=False,
                 provenance_keys=None,
                 dont_parse=False, script=None, observatory=None, comparison_reference=None,
                 original_name=None, trap_exceptions=None, run_fitsverify=False):
        
        self.filename = filename
        self.context = context
        self.check_references = check_references
        self.compare_old_reference = compare_old_reference
        self._dump_provenance_flag = dump_provenance
        self.dont_parse = dont_parse     # mapping only
        self.script = script
        self.observatory = observatory
        self.comparison_reference = comparison_reference
        self.original_name = original_name
        self.trap_exceptions = trap_exceptions
        self.run_fitsverify = run_fitsverify
        self.error_on_exception = log.exception_trap_logger(self.log_and_track_error)

        assert self.check_references in [False, None, "exist", "contents"], \
            "invalid check_references parameter " + repr(self.check_references)

        self.observatory = observatory or utils.file_to_observatory(filename)
    
        self.provenance_keys = list(provenance_keys or utils.get_observatory_package(self.observatory).PROVENANCE_KEYWORDS)

    @property
    def basename(self):
        """Return the basename of the file being certified by this Certifier."""
        return os.path.basename(self.filename)

    @property
    def format_name(self):
        """Return the quoted name of the file being checked,  using the `original_name` substitute
        provided by web interfaces, or else the basename of the file or temporary file being certified.
        """
        return repr(self.original_name) if self.original_name else repr(self.basename)

    @property
    def locator(self):
        """Return the locator module for the observatory this Certifier corresponds to."""
        return utils.get_locator_module(self.observatory)
    
    def log_and_track_error(self, *args, **keys):
        """Output a log error on behalf of `msg`,  tracking it for uniqueness if run inside a script."""
        if self.script:
            self.script.log_and_track_error(self.basename, *args, **keys)
        else:
            log.error("In", repr(self.basename), ":", *args, **keys)
            
    def certify(self):
        """Certify `self.filename`,  either reporting using log.error() or raising
        exceptions.
        """
        raise NotImplementedError("Certify is an abstract class.")


    def get_validators(self):
        """Given a reference file `filename`,  return the observatory specific
        list of Validators used to check that reference file type.
        """
        # Get the cache key for this filetype.
        checkers = validators.get_validators(self.observatory, self.filename)
        checkers = self.set_rmap_parkeys_to_required(checkers)
        return checkers

    @property
    def array_validators(self):
        """Return the list of Validator objects that apply to arrays."""
        return [checker for checker in self.validators if checker.info.keytype in ["A","D"]]

    def set_rmap_parkeys_to_required(self, checkers):
        """Mutate copies of `checkers` so that any specified by the rmap parkey are required."""
        parkeys = set(self.get_rmap_parkeys())
        vlist = []
        for valid in checkers:
            if not valid.optional:
                vlist.append(valid)
            elif valid.name not in parkeys:
                vlist.append(valid)
            else:
                log.verbose("Mapping", repr(valid.name), "to REQUIRED based on rmap parkeys from", 
                            repr(self.get_corresponding_rmap().basename))
                vlist.append(valid.get_required_copy())
        return vlist

    def get_corresponding_rmap(self):
        """Return the rmap which corresponds to self.filename under self.context."""
        pmap = crds.get_pickled_mapping(self.context, ignore_checksum="warn")  # reviewed
        instrument, filekind = pmap.locate.get_file_properties(self.filename)
        return pmap.get_imap(instrument).get_rmap(filekind)

    def get_rmap_parkeys(self):
        """Determine required parkeys in reference path `refname` according to pipeline
        mapping `context`.
        """
        if self.context is None:
            return []
        try:
            return self.get_corresponding_rmap().get_required_parkeys()
        except Exception as exc:
            log.verbose_warning("Failed retrieving required parkeys:", str(exc))
            return []

# ============================================================================

class ReferenceCertifier(Certifier):
    """Baseclass for most reference file certifier classes.    
    1. Check simple keywords against TPN files using the reftype's validators.
    2. Check mode tables against prior reference of comparison_context.
    3. Dump out keywords of interest.
    """
    def __init__(self, *args, **keys):
        super(ReferenceCertifier, self).__init__(*args, **keys)
        self.header = None
        self.validators = None
        self.all_column_names = None
        self.all_simple_names = None
        self.mode_columns = None
        self.types = reftypes.get_types_object(self.observatory)
        
    def complex_init(self):
        """Can't do this until we at least know the file is loadable."""
        self.validators = self.get_validators()
        self.all_column_names = [ val.name for val in self.validators if val.info.keytype == 'C' ]
        self.all_simple_names = [ val.name for val in self.validators if val.info.keytype != 'C' ]
        self.mode_columns = self.get_mode_column_names()
    
    def certify(self):
        """Certify `self.filename`,  either reporting using log.error() or raising
        ValidationError exceptions.
        """
        try:
            self.complex_init()
        except TypeSetupError as exc:
            log.verbose_warning("Error locating constraints for", repr(self.format_name), ":", str(exc))
        except Exception as exc:
            raise
        with self.error_on_exception("Error loading"):
            self.header = self.load()
        if not self.header:
            return
        log.verbose("Header:", log.PP(self.header), verbosity=55)
        for checker in self.validators:
            with self.error_on_exception("Checking", repr(checker.info.name)):
                log.verbose("Checking", checker, verbosity=70)
                checker.check(self.filename, self.header)                
        with self.error_on_exception("Checking", repr(checker.info.name)):
            if self.mode_columns:
                self.certify_reference_modes()
            if self._dump_provenance_flag:
                self.dump_provenance()
    
    def load(self):
        """Load and parse header from self.filename."""
        header = data_file.get_header(self.filename, observatory=self.observatory, original_name=self.original_name)
        header = self.map_reference_keywords_to_dataset_keywords(header)
        header = self.cross_strap_instrument_keywords(header)
        header = self.add_array_keywords(header)
        header = abstract.ensure_keys_defined(header, needed_keys=[checker.complex_name for checker in self.validators])
        return header
    
    def map_reference_keywords_to_dataset_keywords(self, header):
        """Based on the rmap corresponding to this reference filename a`header`,  map keywords
        in `header` from the names used in reference files to the corresponding names matched in
        datasets.   Returnes new `header`.
        """
        if self.context:
            rmapping = None
            with log.verbose_warning_on_exception("No corresponding rmap"):
                rmapping = self.get_corresponding_rmap()
            if rmapping:
                with self.error_on_exception("Error mapping reference names and values to dataset names and values"):
                    header = rmapping.locate.reference_keys_to_dataset_keys(rmapping, header)
        return header
    
    def cross_strap_instrument_keywords(self, header):
        """Add all variations of the instrument keyword to `header` based on some variation of
        instrument name defined in `header`.   Mutates `header`.
        """
        header = dict(header)
        instr = utils.header_to_instrument(header)
        for key in crds.INSTRUMENT_KEYWORDS:
            header[key] = instr
        return header

    def add_array_keywords(self, header):
        """Add synthetic array keywords based on properties of the arrays mentioned in
        array validators to header.   Muates `header`.
        """
        header = dict(header)
        for checker in self.array_validators:
            array_name = checker.complex_name
            # None is untried,  UNDEFINED is tried and failed.
            if header.get(array_name, None) == "UNDEFINED":
                continue
            if ((array_name not in header) or 
                (checker.info.keytype=="D" and header[array_name]["DATA"] is None)):
                header[array_name] = data_file.get_array_properties(self.filename, checker.name, checker.info.keytype)
        return header

    def dump_provenance(self):
        """Dump out provenance keywords for informational purposes."""
        dump_keys = sorted(
            set(key.upper() for key in 
                self.get_rmap_parkeys() + # what's matched,  maybe not .tpn
                self.all_simple_names +   # what's defined in .tpn's, maybe not matched
                self.provenance_keys))    # extra project-specific keywords like HISTORY, COMMENT, PEDIGREE
        unseen = self._dump_provenance_core(dump_keys)
        log.verbose("Potential provenance keywords:", repr(dump_keys), verbosity=80)
        warn_keys = self.provenance_keys
        for key in sorted(unseen):
            if key in warn_keys:
                log.warning("Missing keyword '%s'."  % key)

    def _dump_provenance_core(self, dump_keys):
        """Generic dumper for self.header,  returns unseen keys."""
        unseen = set(dump_keys)
        for key in sorted(dump_keys):
            if self._check_provenance_key(key):
                unseen.remove(key)
        return unseen

    def _check_provenance_key(self, key):
        """Check one keyword, dump it,  and return True IFF it was present in self.header."""
        hval = self.header.get(key, None)
        if hval is not None:
            if self.interesting_value(hval):
                log.info(key, "=", repr(hval))
            return True
        else:
            return False

    def interesting_value(self, value):
        """Return True IFF `value` isn't uninteresting."""
        if str(value).strip().lower() in \
                ["",
                 "*** end of mandatory fields ***",
                 "*** column names ***",
                 "*** column formats ***"]:
            return False
        return True

    def get_mode_column_names(self):
        """Return any column names of `self` defined to be mode columns by the corresponding rmap in `self.context`.
        
        Only tables whose rmaps define row_keys will have mode checking performed.
        
        The first iteration of row_keys were defined as an rmap header paramter.  Subsequent iterations switched
        to a global definition in the locator module file rowkeys.dat.   The current iteration defines rowkeys in
        the spec for each type in the observatory package.
        """
        mode_columns = []
        with self.error_on_exception("Error finding unique row keys for", repr(self.basename)):
            instrument, filekind = utils.get_file_properties(self.observatory, self.filename)
            mode_columns = self.types.get_row_keys(instrument, filekind)
            if mode_columns:
                log.info("Table unique row parameters defined as", repr(mode_columns))
            else:
                log.verbose("No unique row parameters, skipping table row checks.")
        return mode_columns
            
    def certify_reference_modes(self):
        """Check column parameters row-by-row, using mode groups."""
        if self.comparison_reference:
            old_reference = self.comparison_reference
        else:
            if self.context is not None:
                old_reference = self.find_old_reference(self.context, self.filename)
            else:
                old_reference = None
            if old_reference is None or old_reference == self.basename:
                # Load tables modes anyway,  looking for duplicate modes.
                for tab in tables.tables(self.filename):
                    table_mode_dictionary("new reference", tab, self.mode_columns)
                log.warning("No comparison reference for", repr(self.basename), 
                            "in context", repr(self.context) + ". Skipping tables comparison.")
                return
        n_old_segments = tables.ntables(old_reference)
        n_new_segments = tables.ntables(self.filename)
        if n_old_segments != n_new_segments:
            log.warning("Differing HDU counts in", repr(old_reference), "and", repr(self.basename), ":",
                        n_old_segments, "vs.", n_new_segments)
            
        old_tables = tables.tables(old_reference)
        new_tables = tables.tables(self.filename)

        for i in range(0, min(n_new_segments, n_old_segments)):
            with self.error_on_exception("Checking tables modes in segment", i, "of", repr(self.filename)):
                self.check_table_modes(old_tables[i], new_tables[i])
    
    def find_old_reference(self, context, reffile):
        """Returns the name of the old reference file(s) that the new reffile would replace in `context`,  or None.
        """
        log.verbose("Resolving comparison reference for", repr(reffile), "in context", repr(context))
        with log.warn_on_exception("Failed resolving comparison reference for table checks"):
            return self._find_old_reference(context, reffile) 
    
    def _find_old_reference(self, context, reffile):
        """Returns the name of the old reference file(s) that the new reffile would replace."""
        
        reference_mapping = find_governing_rmap(context, reffile)
        
        refname = os.path.basename(reffile)
        if refname in reference_mapping.reference_names():
            return refname
    
        # Determine the corresponding reference by attempting to add reffile to the old context.
        new_r = reference_mapping.insert_reference(reffile)
        
        # Examine the differences and treat the replaced file as the prior reference.
        diffs = reference_mapping.difference(new_r)
        match_refname = None
        for diff_tup in diffs:
            if diff.diff_action(diff_tup) == "replace":
                match_refname, dummy = diff.diff_replace_old_new(diff_tup)
                assert dummy == refname, "Bad replacement inserting '{}' into '{}'".format(reffile, reference_mapping.name)
                break
        else:
            log.info("No file corresponding to", repr(reffile), "in context", repr(reference_mapping.name))
            return None
        
        # grab match_file from server and copy it to a local disk, if network
        # connection is available and configured properly
        # Note: this call works in both networked and non-networked modes of operation.
        # Non-networked mode requires access to /grp/crds/[hst|jwst] or a copy of it.
        try:
            match_files = api.dump_references(reference_mapping.name, baserefs=[match_refname], ignore_cache=False)
            match_file = match_files[match_refname]
            if not os.path.exists(match_file):   # For server-less mode in debug environments w/o Central Store
                raise IOError("Comparison reference " + repr(match_refname) + " is defined but does not exist.")
            log.info("Comparing reference", repr(refname), "against", repr(os.path.basename(match_file)))
        except Exception as exc:
            log.warning("Failed to obtain reference comparison file", repr(match_refname), ":", str(exc))
            match_file = None
    
        return match_file
    
    def check_table_modes(self, old_table, new_table):
        """Check the tables modes of extension `ext` of `old_reference` versus self.filename"""
        new_reference_ex = new_table.basename + "[" + str(new_table.segment) + "]"
        old_reference_ex = old_table.basename + "[" + str(old_table.segment) + "]"
        log.verbose("Checking tables modes of '{}' against comparison reference '{}'".format(
            new_reference_ex, old_reference_ex))
        old_modes, old_all_cols = table_mode_dictionary("old reference", old_table, self.mode_columns)
        if not old_modes:
            log.info("No modes defined in comparison reference", repr(old_reference_ex), 
                     "for keys", repr(self.mode_columns))
            return
        new_modes, new_all_cols = table_mode_dictionary("new reference", new_table, self.mode_columns)
        if not new_modes:
            log.info("No modes defined in new reference", repr(new_reference_ex), "for keys", 
                     repr(self.mode_columns))
            return
        old_sample = list(old_modes.values())[0]
        new_sample = list(new_modes.values())[0]
        if len(old_sample) != len(new_sample) or old_all_cols != new_all_cols:
            log.warning("Change in row format betwween", repr(old_reference_ex), "and", repr(new_reference_ex))
            log.verbose("Old sample:", repr(old_sample))
            log.verbose("New sample:", repr(new_sample))
            return
        for mode in sorted(old_modes):
            if mode not in new_modes:
                log.warning("Table mode", mode, "from old reference", repr(old_reference_ex),
                            "is NOT IN new reference", repr(new_reference_ex))
                log.verbose("Old:", repr(old_modes[mode]), verbosity=60)
                continue
            # modes[mode][0] is row_no,  modes[mode][1] is row value
            diffs = self.compare_row_values(mode, old_modes[mode][1], new_modes[mode][1])
            if not diffs:
                log.verbose("Mode", mode, "of", repr(new_reference_ex), 
                            "has same values as", repr(old_reference_ex),  verbosity=60)
            else:
                log.verbose("Mode change", mode, "between", repr(old_reference_ex), "and", 
                            repr(new_reference_ex))
                log.verbose("Old:", repr(old_modes[mode]), verbosity=60)
                log.verbose("New:", repr(new_modes[mode]), verbosity=60)
        for mode in sorted(new_modes):
            if mode not in old_modes:
                log.info("Table mode", mode, "of new reference", repr(new_reference_ex),
                         "is NOT IN old reference", repr(old_table.basename))
                log.verbose("New:", repr(new_modes[mode]), verbosity=60)
                
    def compare_row_values(self, mode, old_row, new_row):
        """Compare key value tuple list `old_row` to `new_row` for key value tuple list `mode`.
        Handle array value comparisons.   
        
        Return 0 if old_row == new_row,  non-0 otherwise.
        """
        different = 0
        for field_no, (old_key, old_value) in enumerate(old_row):
            new_key, new_value = new_row[field_no]
            if old_key != new_key:
                log.warning("Column key mismatch at mode", mode, "old_key", repr(old_key), 
                            "new_key", new_key)
                different += 1
            old_value = handle_nan(old_value)
            new_value = handle_nan(new_value)
            if np.any(old_value != new_value):
                different += 1
        return different

# ============================================================================

def find_governing_rmap(context, reference):
    """Given mapping `context`,  return the loaded rmap which governs `reference`.   Typically this will
    be the rmap which contains the predecessor to `reference`,  not `reference` itself.
    """
    mapping = rmap.asmapping(context, cached=True)
    instrument, filekind = mapping.locate.get_file_properties(reference)
    if mapping.name.endswith(".pmap"):
        governing_rmap = mapping.get_imap(instrument).get_rmap(filekind)
    elif mapping.name.endswith(".imap"):
        governing_rmap = mapping.get_rmap(filekind)
    elif mapping.name.endswith(".rmap"):
        governing_rmap = mapping
    else:
        raise ValueError("Invalid comparison context " + repr(context))
    g_instrument, g_filekind = mapping.locate.get_file_properties(governing_rmap.name)
    assert instrument == g_instrument, "Comparison context inconsistent with reference file: " + repr(instrument) + " vs. " + repr(g_instrument)
    assert filekind == g_filekind, "Comparison context inconsistent with reference type: " + repr(filekind) + " vs. " + repr(g_filekind)
    log.verbose("Reference '{}' corresponds to rmap '{}' in context '{}'".format(
        reference, governing_rmap.name, mapping.name))
    return governing_rmap

# ============================================================================

def table_mode_dictionary(generic_name, tab, mode_keys):
    """Returns ({ (mode_val,...) : (row_no, (entire_row_values, ...)) },  [col_name, ...] ) 
    for crds.tables `tab` where column names `mode_keys` define the  columns to select for mode values.
    """
    all_cols = [name.upper() for name in tab.colnames]
    basename = repr(os.path.basename(tab.filename) + "[{}]".format(tab.segment))
    log.verbose("Mode columns for", generic_name, basename, "are:", repr(mode_keys))
    log.verbose("All column names for", generic_name, basename, "are:", repr(all_cols))
    modes = defaultdict(list)
    for i, row in enumerate(tab.rows):
        new_row = tuple(zip(all_cols, (handle_nan(v) for v in row)))
        rowdict = dict(new_row)
        # Table row keys can vary by extension.  Have CRDS support a simple model of using
        # whichever mode_keys are present in a given row.
        mode = tuple((key, rowdict[key]) for key in mode_keys if key in rowdict)
        if not mode:
            log.info("Empty actual mode in", generic_name, basename, "with candidate mode columns", mode_keys)
            return {}, []
        modes[mode].append((i, new_row))
    for mode in sorted(modes.keys()):
        if len(modes[mode]) > 1:
            log.warning("Duplicate definitions in", generic_name, basename, "for mode:", mode, ":\n", 
                        "\n".join([repr(row) for row in modes[mode]]))
    # modes[mode][0] is first instance of multiply defined mode.
    return { mode:modes[mode][0] for mode in modes }, all_cols

def handle_nan(var):
    """Map nan values to 'nan' so that 'nan' == 'nan'."""
    if isinstance(var, (np.float32, np.float64, np.float128)) and np.isnan(var):
        return 'nan'
    elif isinstance(var, np.ndarray) and var.shape == () and np.any(np.isnan(var)):
        return 'nan'
    else:
        return var
    
# ============================================================================

class FitsCertifier(ReferenceCertifier):
    """Certifier dedicated to FITS format references."""

    def __init__(self, *args, **keys):
        super(FitsCertifier, self).__init__(*args, **keys)
        if self.run_fitsverify:
            status, out = pysh.status_out_err("which fitsverify")
            if status == 0:
                log.verbose("fitsverify enabled and installled at", repr(out))
            else:
                log.warning("External fitsverify program is enabled but not found on PATH.")
                self.run_fitsverify = False

    def load(self):
        """Use pyfits to verify the FITS format of self.filename."""
        if not self.filename.endswith(".fits"):
            log.verbose("Skipping FITS verify for '%s'" % self.basename)
            return
        with data_file.fits_open_trapped(self.filename, checksum=bool(config.FITS_VERIFY_CHECKSUM)) as pfile:
            pfile.verify(option='exception') # validates all keywords
        log.info("FITS file", repr(self.basename), "conforms to FITS standards.")
        return super(FitsCertifier, self).load()

    def _dump_provenance_core(self, dump_keys):
        """FITS provenance dumper,  works on multiple extensions.  Returns unseen keys."""
        with data_file.fits_open_trapped(self.filename) as hdulist:
            unseen = set(dump_keys)
            for i, hdu in enumerate(hdulist):
                for key in dump_keys:
                    for card in hdu.header.cards:
                        if card.keyword == key:
                            if self.interesting_value(card.value):
                                log.info("["+str(i)+"]", key, card.value, card.comment)
                            if key in unseen:
                                unseen.remove(key)
        unseen = super(FitsCertifier, self)._dump_provenance_core(unseen)
        return unseen

    def certify(self):
        """Run checks on FITS file."""
        super(FitsCertifier, self).certify()
        if self.run_fitsverify:
            self.fitsverify()
    
    def fitsverify(self):
        """Run optional external fitsverify program from cfitsio library, installed separately from CRDS."""
        log.info("Running fitsverify.")
        err, output = pysh.status_out_err("fitsverify {}".format(self.filename))
        for line in output.splitlines():
            if "Error:" in line:
                log.error(">>", line)
            elif "Warning:" in line:
                log.warning(">>", line)
            else:
                log.info(">>", line)
        grade_fitsverify_output(err, output)

def grade_fitsverify_output(status, output):
    """Issue log error or warning messages based on the exit status and output
    returned by fitsverify.
    """
    m = re.search(r"(\d+)\s+error\(s\)", output)
    if m and m.groups()[0] != "0" or "checksum is not" in output:
        log.error("Errors or checksum warnings in fitsverify log output.")
    elif status:
        log.warning("Errors or warnings indicated by fitsverify exit status.")

# ============================================================================

class UnknownCertifier(Certifier):
    """Certifier for unknown type,  currently a pass through with a warning."""
    
    def certify(self):
        """Certify an unknown format file."""
        log.warning("No certifier defined for", repr(self.basename))
        with log.augment_exception("Error parsing ", exception_class=InvalidFormatError):
            self.load()

    def load(self):
        """Load file of unknown type."""
        with open(self.filename, "rb") as handle:
            contents = handle.read()
        return contents
    
# ============================================================================

class MappingCertifier(Certifier):
    """Parameter container for certifying a mapping file,  and possibly it's references."""

    def certify(self):
        """Certify mapping `self.filename` relative to `self.context`."""
        if not self.dont_parse:
            parsing = mapping_parser.parse_mapping(self.filename)
            mapping_parser.check_duplicates(parsing)

        mapping = rmap.fetch_mapping(self.filename, ignore_checksum="warn")
        mapping.validate_mapping()
    
        # derived_from = mapping.get_derived_from()
        derived_from = find_old_mapping(self.context, self.filename)
        if derived_from is not None:
            if derived_from.name == self.basename:
                log.verbose("Mapping", repr(self.filename), "did not change relative to context", repr(self.context))
            else:
                log.info("Mapping", repr(self.basename), "corresponds to", repr(derived_from.name),
                         "from context", repr(self.context), "for checking mapping differences.")
                diff.mapping_check_diffs(mapping, derived_from)
        else:
            if self.context is not None:
                log.info("No predecessor for", repr(mapping.name), "relative to context", repr(self.context))
            
        # Optionally check nested references,  only for rmaps.
        if not self.check_references or not mapping.specifies_references:
            return

        references = self.get_existing_reference_paths(mapping)
        
        if self.check_references == "contents":
            certify_files(references, context=self.context, 
                          dump_provenance=self._dump_provenance_flag,
                          check_references=self.check_references,
                          compare_old_reference=self.compare_old_reference,
                          trap_exceptions=self.trap_exceptions,
                          script=self.script,
                          observatory=self.observatory,
                          run_fitsverify=self.run_fitsverify)

    def get_existing_reference_paths(self, mapping):
        """Return the paths of the references referred to by mapping.  Omit
        paths for which the reference does not exist.
        """
        references = []
        for ref in mapping.reference_names():
            path = None
            with self.error_on_exception("Can't locate reference file", repr(ref)):
                path = get_existing_path(ref, mapping.observatory)
            if path:
                log.verbose("Reference", repr(ref), "exists at", repr(path))
                references.append(path)
        return references
    
def get_existing_path(reference, observatory):
    """Return the path of `reference` located relative to `mapping`."""
    path = rmap.locate_file(reference, observatory)
    if not os.path.exists(path):
        raise ValidationError("Path " + repr(path) + " does not exist.")
    return path

def find_old_mapping(comparison_context, new_mapping):
    """Find the Mapping in pmap `comparison_context` corresponding to filename `new_mapping`,  if there is one.
    This call will cache `comparison_context` so it should only be called on "official" mappings,  not
    trial mappings.
    """
    if comparison_context:
        comparison_mapping = crds.get_pickled_mapping(comparison_context)  # reviewed
        old_mapping = comparison_mapping.get_equivalent_mapping(new_mapping)
        return old_mapping
    else:
        return None

def banner(char='#'):
    """Print a standard divider."""
    log.info(char * 40)  # Serves as demarkation for each file's report
    
# ============================================================================

@data_file.hijack_warnings
def certify_file(filename, context=None, dump_provenance=False, check_references=False, 
                 trap_exceptions=True, compare_old_reference=False,
                 dont_parse=False, script=None, observatory=None,
                 comparison_reference=None, original_name=None, ith="",
                 run_fitsverify=False):
    """Certify the list of `files` relative to .pmap `context`.   Files can be
    references or mappings.   This function primarily provides an interface for web code.
    
    filename:               path of file to certify
    context:                .pmap name to certify relative to
    dump_provenance:        for references,  log provenance keywords and rmap parkey values.
    check_references:       False, "exists", "contents"
    compare_old_reference:  bool,  if True,  attempt tables mode checking.
    dont_parse:             bool,  if True,  don't run parser to scan mappings for duplicate keys.
    script:                 command line Script instance
    trap_exceptions:        if True, trapped exceptions issue ERROR messages. Otherwise reraised.
    original_name:          browser-side name of file if any, files 
    """    
    try:
        old_flag = log.set_exception_trap(trap_exceptions)    #  XXX non-reentrant code,  no threading
        
        if original_name is None:
            original_name = filename
            
        if observatory is None:
            observatory = utils.file_to_observatory(filename)

        filetype, klass = get_certifier_class(original_name, filename)

        if comparison_reference:
            log.info("Certifying", repr(original_name) + ith,  "as", repr(filetype.upper()),
                     "relative to context", repr(context), "and comparison reference", repr(comparison_reference))
        else:
            log.info("Certifying", repr(original_name) + ith, "as", repr(filetype.upper()),
                     "relative to context", repr(context))

        trap = log.error_on_exception if script is None else script.error_on_exception
            
        with trap(filename, "Certifier instantiation error"):
            certifier = klass(filename, context=context, check_references=check_references,
                              compare_old_reference=compare_old_reference,
                              dump_provenance=dump_provenance,
                              dont_parse=dont_parse, script=script, observatory=observatory,
                              comparison_reference=comparison_reference,
                              original_name=original_name,
                              trap_exceptions=trap_exceptions,
                              run_fitsverify=run_fitsverify)
        with trap(filename, "Validation error"):
            certifier.certify()

    finally:
        log.set_exception_trap(old_flag)
        gc.collect()

def get_certifier_class(original_name, filepath):
    """Given a reference file name with a valid extension, return the filetype and 
    Certifier subclass used to check it.
    """
    klasses = {
        "mapping" : MappingCertifier,
        "fits" : FitsCertifier,
        "json" : ReferenceCertifier,
        "yaml" : ReferenceCertifier,
        "asdf" : ReferenceCertifier,
        "geis" : ReferenceCertifier,
        "unknown" : UnknownCertifier,
    }
    filetype = data_file.get_filetype(filepath, original_name)
    klass = klasses.get(filetype, UnknownCertifier)
    return filetype, klass
        
# @data_file.hijack_warnings
def certify_files(files, context=None, dump_provenance=False, check_references=False, 
                  trap_exceptions=True, compare_old_reference=False,
                  dont_parse=False, skip_banner=False, script=None, observatory=None,
                  comparison_reference=None, run_fitsverify=False):
    """certify_files() core function with error trapping set."""
    
    for fnum, filename in enumerate(files):
        
        if not skip_banner:
            banner()
        
        ith = ' (' + str(fnum+1) + '/' + str(len(files)) + ')'
        
        certify_file(
            filename, context=context, dump_provenance=dump_provenance, check_references=check_references, 
            trap_exceptions=trap_exceptions, compare_old_reference=compare_old_reference, 
            dont_parse=dont_parse, script=script, observatory=observatory,
            comparison_reference=comparison_reference, ith=ith, run_fitsverify=run_fitsverify)
        
    tables.clear_cache()
    if not skip_banner:
        banner()

def test():
    """Run doctests in this module.  See also certify unittests."""
    import doctest
    from crds import certify
    return doctest.testmod(certify)

# ============================================================================

class CertifyScript(cmdline.Script, cmdline.UniqueErrorsMixin):
    """Command line script for checking CRDS mapping and reference files.
    
    Perform checks on each of `files`.   Print status.   If file is a context /
    mapping file,  it is used to define associated reference files which are
    located on the CRDS server.  If file is a .fits file,  it should include a
    relative or absolute filepath.
    """
    
    def __init__(self, *args, **keys):
#        super(CertifyScript, self).__init__(*args, **keys)
        if "print_status" not in keys:
            keys["print_status"] = True
        cmdline.Script.__init__(self, *args, **keys)
        cmdline.UniqueErrorsMixin.__init__(self, *args, **keys)

    description = """
Checks a CRDS reference or mapping file:

1. Verifies basic file format: .fits, .json, .yaml, .asdf, .pmap, .imap, .rmap
2. Checks references for required keywords and values, where constraints are defined.
3. Checks CRDS rules for permissible values with respect to defined reference constraints.
4. Checks CRDS rules for accidental file reversions or duplicate lines.
5. Checks CRDS rules for noteworthy version-to-version changes such as new or removed match cases.
6. Checks tables for deleted or duplicate rows relative to a comparison table.
7. Finds comparison references with respect to old CRDS contexts.
    """
    
    epilog = """
    
To run crds.certify on a reference(s) to verify basic file format and parameter constraints:

  % python -m crds.certify --comparison-context=hst_0027.pmap   some_reference.fits...

If some_reference.fits is a table,  a comparison table will be found in the comparison context, if appropriate.

For recursively checking CRDS rules do this:

  % python -m crds.certify hst_0311.pmap --comparison-context=hst_0312.pmap

If a comparison context is defined, checked mappings will be compared against their peers (if they exist) in
the comparison context.  Many classes of mapping differences will result in warnings.

For reference table checks,  a comparison reference can also be specified directly rather than inferred from context:

  % python -m crds.certify some_reference.fits --comparison-reference=old_reference_version.fits

For more information on the checks being performed,  use --verbose or --verbosity=N where N > 50.
    """
    
    def add_args(self):
        self.add_argument("files", nargs="+")
        self.add_argument("-d", "--deep", dest="deep", action="store_true",
                          help="Certify reference files referred to by mappings have valid contents.")
        self.add_argument("-r", "--dont-recurse-mappings", dest="dont_recurse_mappings", action="store_true",
                          help="Do not load and validate mappings recursively,  checking only directly specified files.")
        self.add_argument("-a", "--dont-parse", dest="dont_parse", action="store_true",
                          help="Skip slow mapping parse based checks,  including mapping duplicate entry checking.")
        self.add_argument("-e", "--exist", dest="exist", action="store_true",
                          help="Certify reference files referred to by mappings exist.")
        self.add_argument("-p", "--dump-provenance", dest="dump_provenance", action="store_true",
                          help="Dump provenance keywords.")
        self.add_argument("-x", "--comparison-context", dest="comparison_context", type=str, default=None,
                          help="Pipeline context defining comparison files.  Defaults to operational context,  use 'none' to suppress.")
        self.add_argument("-y", "--comparison-reference", dest="comparison_reference", type=str, default=None,
                          help="Comparison reference for tables certification.")
        self.add_argument("-s", "--sync-files", dest="sync_files", action="store_true",
                          help="Fetch any missing files needed for the requested difference from the CRDS server.")
        self.add_argument("-l", "--allow-schema-violations", action="store_true",
                          help="Report jwst.datamodels schema violations as warnings rather than as errors.")
        self.add_argument("-f", "--run-fitsverify", action="store_true",
                          help="Run fitsverify for additional external checks on FITS files. cfitsio library must be installed separately.")
        
        cmdline.UniqueErrorsMixin.add_args(self)
        
    # For files on the command line to default to normal UNIX syntax, no path is CWD,
    # uncomment following statement.   Add crds:// for cache paths.

    # locate_file = cmdline.Script.locate_file_outside_cache

    def main(self):
        if self.args.deep:
            check_references = "contents"
        elif self.args.exist:
            check_references = "exist"
        else:
            check_references = None

        if self.args.allow_schema_violations:
            config.ALLOW_SCHEMA_VIOLATIONS.set(True)

        # String spellings of "none" are from command line,  None is the default which means "use operational context".
        assert (self.args.comparison_context in [None, "none", "NONE", "None"]) or config.is_mapping_spec(self.args.comparison_context), \
            "Specified --context file " + repr(self.args.comparison_context) + " is not a CRDS mapping."

        assert (self.args.comparison_reference is None) or not config.is_mapping_spec(self.args.comparison_reference), \
            "Specified --comparison-reference file " + repr(self.args.comparison_reference) + " is not a reference."
            
        if self.args.comparison_context and self.args.sync_files:
            resolved_context = self.resolve_context(self.args.comparison_context)
            self.sync_files([resolved_context])
        if self.args.comparison_reference and self.args.sync_files:
            self.sync_files([self.args.comparison_reference])
            
        if not self.args.dont_recurse_mappings:
            all_files = self.mapping_closure(self.files)
        else:
            all_files = set(self.files)
            
        if not self.are_all_references(all_files) and not self.are_all_mappings(all_files):
            if self.args.comparison_context is None and not self.args.comparison_reference:
                log.info("Mixing references and mappings in one certify run skips any default comparison checks.")

        if self.are_all_references(all_files):
            # Change original default behavior of None to default operational context,  for references.
            # For mappings / older contexts the default tends to be the wrong thing,  hence references only.
            if self.args.comparison_context is None and not self.args.comparison_reference:
                log.verbose("Defaulting comparison context to latest operational CRDS context.")
                self.args.comparison_context = self.default_context
        elif self.args.comparison_context in [None, "none", "None", "NONE"]:
            log.info("No comparison context specified or specified as 'none'.  No default context for all mappings or mixed types.")
            self.args.comparison_context = None
            
        if self.args.comparison_reference:
            comparison_reference = config.locate_reference(self.args.comparison_reference, self.observatory)
        else:
            comparison_reference = None
            
        certify_files(sorted(all_files), 
                      context=self.resolve_context(self.args.comparison_context),
                      comparison_reference=comparison_reference,
                      compare_old_reference=self.args.comparison_context or self.args.comparison_reference,
                      dump_provenance=self.args.dump_provenance, 
                      check_references=check_references, 
                      dont_parse=self.args.dont_parse,
                      trap_exceptions = not self.args.debug_traps,
                      script=self, observatory=self.observatory,
                      run_fitsverify=self.args.run_fitsverify)
    
        self.dump_unique_errors()
        return log.errors()
    
    def log_and_track_error(self, filename, *args, **keys):
        """Override log_and_track_error() to compute instrument, filekind automatically."""
        try:
            instrument, filekind = utils.get_file_properties(self.observatory, filename)
        except Exception:
            instrument = filekind = "unknown"
        super(CertifyScript, self).log_and_track_error(filename, instrument, filekind, *args, **keys)
        return None  # to suppress re-raise
    
    def mapping_closure(self, files):
        """Traverse the mappings in `files` and return a list of all mappings referred to by 
        `files` as well as any references in `files`.
        """
        closure_files = set()
        for file_ in files:
            more_files = {file_}
            if rmap.is_mapping(file_):
                with self.error_on_exception(file_, "Problem loading submappings of", repr(file_)):
                    mapping = crds.get_cached_mapping(file_, ignore_checksum="warn")
                    more_files = {rmap.locate_mapping(name) for name in mapping.mapping_names()}
                    more_files = (more_files - {rmap.locate_mapping(mapping.basename)}) | {file_}
            closure_files |= more_files
        return sorted(closure_files)


