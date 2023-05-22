from collections import defaultdict
from django.db.models import F

import requests
from reference_data.models import Omim, GeneConstraint, GENOME_VERSION_LOOKUP
from seqr.models import Individual, Sample, PhenotypePrioritization
from seqr.utils.search.constants import RECESSIVE, COMPOUND_HET, MAX_NO_LOCATION_COMP_HET_FAMILIES, PRIORITIZED_GENE_SORT
from seqr.views.utils.orm_to_json_utils import get_json_for_queryset


def get_hail_variants(samples, search, user, previous_search_results, genome_version, sort=None, page=1, num_results=100,
                      gene_agg=False, skip_genotype_filter=False):
    if skip_genotype_filter:
        raise NotImplementedError

    sample_data = _get_sample_data(samples, search.get('inheritance_filter'))

    end_offset = num_results * page
    search_body = {
        'requester_email': user.email,
        'sample_data': sample_data,
        'genome_version': genome_version,
        'sort': sort,
        'sort_metadata': _get_sort_metadata(sort, sample_data),
        'num_results': end_offset,
    }
    search_body.update(search)
    search_body.update({
        'frequencies': search_body.pop('freqs', None),
        'quality_filter': search_body.pop('qualityFilter', None),
        'custom_query': search_body.pop('customQuery', None),
    })
    search_body.pop('skipped_samples', None)

    _parse_location_search(search_body, sample_data)

    path = 'gene_counts' if gene_agg else 'search'
    response = requests.post(f'http://hail-search:5000/{path}', json=search_body)
    response.raise_for_status()
    response_json = response.json()

    previous_search_results['total_results'] = response_json['total']
    previous_search_results['all_results'] = response_json['results']
    return response_json['results'][end_offset - num_results:end_offset]


def _get_sample_data(samples, inheritance_filter):
    sample_data = samples.values(
        'sample_id', 'dataset_type', 'sample_type',
        individual_guid=F('individual__guid'),
        family_guid=F('individual__family__guid'),
        project_guid=F('individual__family__project__guid'),
        affected=F('individual__affected'),
        sex=F('individual__sex'),
    )

    if (inheritance_filter or {}).get('affected'):
        for s in sample_data:
            s['affected'] = inheritance_filter['affected'].get(s['individual_guid']) or s['affected']

    sample_data_by_data_type = defaultdict(list)
    for s in sample_data:
        dataset_type = s.pop('dataset_type')
        sample_type = s.pop('sample_type')
        data_type_key = f'{dataset_type}_{sample_type}' if dataset_type == Sample.DATASET_TYPE_SV_CALLS else dataset_type
        sample_data_by_data_type[data_type_key].append(s)

    return sample_data_by_data_type


def _get_sort_metadata(sort, sample_data):
    sort_metadata = None
    if sort == 'in_omim':
        sort_metadata = Omim.objects.filter(phenotype_mim_number__isnull=False).values_list('gene__gene_id', flat=True)
    elif sort == 'constraint':
        sort_metadata = {
            agg['gene__gene_id']: agg['mis_z_rank'] + agg['pLI_rank'] for agg in
            GeneConstraint.objects.values('gene__gene_id', 'mis_z_rank', 'pLI_rank')
        }
    elif sort == PRIORITIZED_GENE_SORT:
        sort_metadata = {
            agg['gene_id']: agg['min_rank'] for agg in PhenotypePrioritization.objects.filter(
                individual__family__guid=next(sample_data.values())[0]['family_guid'], rank__lte=100,
            ).values('gene_id').annotate(min_rank=Min('rank'))
        }
    return sort_metadata


def _parse_location_search(search, sample_data):
    locus = search.pop('locus', None) or {}
    parsed_locus = search.pop('parsedLocus')

    genes = parsed_locus.get('genes') or {}
    intervals = parsed_locus.get('intervals')
    parsed_intervals = None
    if genes or intervals:
        gene_coords = [
            {field: gene[f'{field}{search["genome_version"].title()}'] for field in ['chrom', 'start', 'end']}
            for gene in genes.values()
        ]
        parsed_intervals = ['{chrom}:{start}-{end}'.format(**interval) for interval in intervals or []] + [
            '{chrom}:{start}-{end}'.format(**gene) for gene in gene_coords]

    exclude_locations = locus.get('excludeLocations')
    has_location_search = bool(parsed_intervals) and not exclude_locations
    if not has_location_search:
        projects = set()
        for samples in sample_data.values():
            projects.update({s['project_guid'] for s in samples})
            if len(projects) > 1:
                break
        if len(projects) > 1:
            from seqr.utils.search.utils import InvalidSearchException
            raise InvalidSearchException('Location must be specified to search across multiple projects')

    search.update({
        'intervals': parsed_intervals,
        'exclude_intervals': exclude_locations,
        'gene_ids': None if exclude_locations else list(genes.keys()),
        'variant_ids': parsed_locus.get('parsed_variant_ids'),
        'rs_ids': parsed_locus.get('rs_ids'),
    })
